"""本地優先的 ``!ask`` 問答垂直切片。

接通 QueryPlan、SQLite memory/evidence、本地 snapshot adapters（技術／籌碼／
ETF／macro／基本面）與 Gemini answer。Ask 路徑不偷偷打外網；來源缺失時回傳
不確定，並只保存短期稽核記錄，不把答案升級成長期記憶。
"""

from __future__ import annotations

import json
import hashlib
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.answer_verifier import VerificationResult, verify_answer_payload
from bot.evidence import (
    EvidencePack,
    build_evidence_pack,
    evidence_temporal_status,
    is_conversation_evidence,
    is_evidence_fresh,
    is_evidence_temporally_acceptable,
    make_evidence,
    persist_evidence_items,
)
from bot.llm_analyzer import GeminiClient, extract_json, gemini_call
from bot.memory_store import MemoryStore
from bot.query_plan import QueryPlan, normalize_planner_payload, plan_query
from bot.resource_adapters import (
    LiveTickerAdapter,
    MonthlyRevenueAdapter,
    MopsAdapter,
    ProfessionalKnowledgeAdapter,
    ResourceResult,
    WebSearchAdapter,
)
from bot.source_registry import is_source_document_replayable
from bot.stock_db import (
    ClaimEvidenceRow,
    ClaimRow,
    EvidenceItemRow,
    LlmOutputRow,
    LlmRunEvidenceRow,
    LlmRunRow,
    MemoryItemRow,
    StockDB,
)
from bot.utils import now_tw


UNCERTAIN_ANSWER = "我不確定，目前沒有足夠的可驗證資料。"

# 最終 AskResult.status；collection 與回答結果分離。
ASK_STATUSES = {
    "answered",
    "uncertain",
    "no_evidence",
    "resource_failed",
    "timeout",
    "live_disabled",
    "partial",
    "verification_failed",
}
_RESOURCE_BLOCKING = {
    "timeout",
    "resource_failed",
    "live_disabled",
    "partial",
}
_CONVERSATION_QUERY_MARKERS = (
    "以前說過", "說過什麼", "歷史對話", "對話紀錄", "問過", "剛才問",
    "上一輪", "上次問", "頻道討論", "頻道以前",
)
_RESOURCE_KIND_MAP = {
    "historical_evidence": {"memory", "history", "historical", "conversation"},
    "channel_knowledge": {"memory", "channel", "conversation"},
    "user_memory": {"memory", "user", "preference", "conversation"},
    "fundamental": {
        "fundamental", "monthly_revenue", "valuation_daily", "dividend_history",
        "quarterly_report",
    },
    "technical": {"technical"},
    "chips": {"chips"},
    "etf": {"etf", "etf_holdings"},
    "macro": {"macro"},
    "news": {"news", "news_cache"},
    "ticker_snapshot": {"ticker_snapshot"},
    # MOPS details are intentionally stored as ``kind=news`` for compatibility
    # with the existing evidence schema, but remain a distinct required
    # resource in QueryPlan so official announcements are not hidden inside a
    # generic news fallback.
    "mops": {"news"},
    "taifex": {"taifex_futures"},
}

_MARKET_RESOURCES = {
    "ticker_snapshot", "technical", "fundamental", "chips", "etf", "taifex",
    "macro", "news", "mops", "web_search", "professional_knowledge",
}


def _search_evidence_is_relevant(item: Dict[str, Any], plan: QueryPlan) -> bool:
    """限制明確市場資源的全文搜尋，避免關鍵詞跨域污染 EvidencePack。"""
    requested = set(plan.resource_needs).intersection(_MARKET_RESOURCES)
    if not requested or "historical_evidence" in plan.resource_needs:
        return True
    allowed: set[str] = set()
    for resource in requested:
        allowed.update(_RESOURCE_KIND_MAP.get(resource, {resource}))
    return str(item.get("kind") or "") in allowed


@dataclass
class AskResult:
    query: str
    answer: str
    status: str
    trace_id: str
    plan: QueryPlan
    evidence: EvidencePack
    verification: VerificationResult
    llm_info: Dict[str, Any] = field(default_factory=dict)
    pending_resources: List[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status == "answered" and self.verification.valid

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "answer": self.answer,
            "status": self.status,
            "trace_id": self.trace_id,
            "plan": self.plan.to_dict(),
            "evidence": self.evidence.to_dict(),
            "verification": self.verification.to_dict(),
            "llm_info": self.llm_info,
            "pending_resources": list(self.pending_resources),
            "collection_status": self.evidence.collection_status,
        }


def _row_to_evidence(
    row: EvidenceItemRow,
    *,
    root: Optional[Path] = None,
    db: Optional[StockDB] = None,
) -> Dict[str, Any]:
    try:
        metadata = json.loads(row.metadata_json or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    source_document_id = str(row.source_document_id or "")
    provenance_status = str(metadata.get("provenance_status") or "")
    if source_document_id and root is not None and db is not None:
        source = db.get_source_document(source_document_id)
        if not is_source_document_replayable(root, source):
            source_document_id = ""
            provenance_status = "unresolved"
            metadata["raw_replayable"] = False
    return make_evidence(
        row.kind,
        source=row.source,
        source_ref=row.source_ref,
        title=row.title,
        url=row.url,
        observed_at=row.observed_at,
        as_of=row.as_of,
        published_at=row.published_at,
        retrieved_at=row.retrieved_at,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        excerpt=row.excerpt,
        quality_status=row.quality_status,
        source_document_id=source_document_id,
        provenance_status=provenance_status,
        metadata=metadata,
    ) | {"id": row.evidence_id}


def _registered_source_document_id(db: StockDB, root: Path, candidate: Any) -> str:
    """只把仍可在本地 raw archive 回放的 exact ID 傳入 Evidence。

    canonical row 可能比 raw archive 更長久存在；只檢查字串前綴會把
    已遺失或被改寫的 raw artifact 誤標成 ``exact``，使回答看似可追溯但
    實際無法回放。
    """
    source_id = str(candidate or "").strip()
    if not source_id.startswith("source-doc:"):
        return ""
    try:
        source = db.get_source_document(source_id)
        return (
            source_id
            if source is not None and is_source_document_replayable(root, source)
            else ""
        )
    except Exception:
        return ""


def _memory_to_evidence(row: MemoryItemRow) -> Dict[str, Any]:
    is_conversation = row.memory_type == "conversation_turn"
    return make_evidence(
        "conversation" if is_conversation else "memory",
        source=row.source or "memory_store",
        source_ref=row.source_record_id or row.memory_id,
        title=row.summary or row.subject or row.memory_id,
        observed_at=row.observed_at or row.updated_at,
        excerpt=row.content or row.summary,
        quality_status=row.quality_status,
        provenance_status="unresolved",
        metadata={
            "memory_id": row.memory_id,
            "memory_type": row.memory_type,
            "memory_layer": row.memory_layer,
            "not_market_fact": is_conversation,
        },
    )


def _evidence_matches_resource(item: Dict[str, Any], resource: str) -> bool:
    """判斷證據能否滿足某個 QueryPlan resource。"""
    kind = str(item.get("kind") or "")
    return kind in _RESOURCE_KIND_MAP.get(resource, {resource})


def wants_conversation_context(plan: QueryPlan) -> bool:
    """使用者是否明確要歷史對話上下文（而非市場事實）。"""
    text = f"{plan.normalized_query} {plan.query}"
    if any(marker in text for marker in _CONVERSATION_QUERY_MARKERS):
        return True
    conversation_resources = {"channel_knowledge", "user_memory", "historical_evidence"}
    market_resources = {
        "fundamental", "technical", "chips", "etf", "taifex", "macro",
        "news", "mops", "web_search", "ticker_snapshot",
    }
    needs = set(plan.resource_needs)
    return bool(needs & conversation_resources) and not bool(needs & market_resources)


def derive_collection_status(
    outcomes: List[Dict[str, Any]],
    *,
    missing_resources: List[str],
    usable_count: int,
    item_count: int,
) -> str:
    """由資源結果推導 collection_status；timeout/failed 永不變成 no_evidence。"""
    statuses = [str(row.get("status") or "") for row in outcomes]
    has_timeout = "timeout" in statuses
    has_failed = any(status in {"failed", "resource_failed"} for status in statuses)
    has_live_disabled = "live_disabled" in statuses
    has_partial = "partial" in statuses
    # Adapter ``ok`` is only collection-level success.  Freshness and temporal
    # filtering happen immediately before this function, so the authoritative
    # success signal here is a usable evidence item.  Otherwise a stale live
    # quote can make a required news timeout look like a harmless partial.
    has_success = usable_count > 0
    failure_kinds = sum([has_timeout, has_failed, has_live_disabled])
    if failure_kinds >= 2:
        return "partial"
    if has_timeout and (has_success or has_failed or has_live_disabled):
        return "partial"
    if has_failed and (has_success or has_live_disabled):
        return "partial"
    if has_live_disabled and has_success:
        return "partial"
    # A partial/empty adapter must not hide a hard failure when no usable
    # evidence survived freshness filtering.  Preserve the hard status first;
    # otherwise a stale ticker response could mask a news timeout.
    if has_timeout:
        return "timeout"
    if has_failed:
        return "resource_failed"
    if has_live_disabled:
        return "live_disabled"
    # Adapter 明確回報 partial 時，不可因其他來源成功而把整體降成 ok。
    # partial 表示來源只取得部分內容或無法完成契約，與「查無此事」不同。
    if has_partial:
        return "partial"
    if missing_resources and usable_count == 0:
        return "no_evidence"
    if item_count == 0 and usable_count == 0:
        return "no_evidence"
    return "ok"


def derive_ask_status(
    *,
    collection_status: str,
    has_usable: bool,
    has_items: bool,
    required_missing: List[str],
    llm_enabled: bool,
    verification: VerificationResult,
    answer: str,
    answered: bool,
    verification_attempted: bool,
) -> str:
    """合併 collection 與回答驗證結果；資源失敗優先於 no_evidence。"""
    if answered:
        return "uncertain" if str(answer).startswith("我不確定") else "answered"
    if verification_attempted and not verification.valid:
        return "verification_failed"
    # 必要資源仍缺且 collection 已說明失敗／逾時／未啟用時，不可降成 no_evidence。
    if required_missing and collection_status in _RESOURCE_BLOCKING:
        return collection_status
    if has_usable and not llm_enabled:
        return "uncertain"
    if required_missing or not has_usable:
        if collection_status in _RESOURCE_BLOCKING:
            return collection_status
        if not has_usable and not has_items:
            return "no_evidence" if collection_status in {"ok", "no_evidence"} else collection_status
        if collection_status == "no_evidence":
            return "no_evidence"
        return "uncertain"
    return "uncertain"


class AskEngine:
    """以 SQLite 為真實來源的問答執行器。"""

    def __init__(
        self,
        *,
        root: Path,
        db: Optional[StockDB] = None,
        client: Optional[GeminiClient] = None,
        use_llm_planner: bool = True,
        enable_live_sources: Optional[bool] = None,
        live_budget_seconds: float = 8.0,
    ) -> None:
        self.root = Path(root)
        self._owns_db = db is None
        self.db = db or StockDB.open(root=self.root)
        self.client = client
        self.use_llm_planner = use_llm_planner
        self.enable_live_sources = (
            bool(enable_live_sources)
            if enable_live_sources is not None
            else str(os.getenv("STOCK_ASK_LIVE", "0")).strip() == "1"
        )
        self.live_budget_seconds = max(1.0, float(live_budget_seconds))
        self.memory = MemoryStore(self.db, actor="ask")

    def close(self) -> None:
        """關閉由 AskEngine 自己建立的 SQLite handle。"""
        if self._owns_db:
            self.db.close()
            self._owns_db = False

    def _infer_ticker_from_local_names(self, query: str) -> str:
        """只用既有 SQLite 名冊從自然語句反查 ticker，不觸發網路抓取。

        CLI 使用者常輸入「台積電最近消息」而不另外傳 ``--ticker``；若不補這
        一步，問答會漏掉 ticker snapshot／MOPS 等個股資源。名稱有歧義時保留
        空值，讓 planner 不因猜測而污染證據範圍。
        """
        text = str(query or "").replace("\u3000", " ").strip()
        if len(text) < 2:
            return ""
        try:
            rows = self.db.list_stock_info()
        except Exception:
            return ""
        matches: Dict[str, int] = {}
        for row in rows:
            symbol = str(row.symbol or "").strip().upper()
            if not symbol:
                continue
            for raw_name in (row.short_name, row.name):
                name = str(raw_name or "").replace("\u3000", " ").strip()
                if len(name) >= 2 and name in text:
                    matches[symbol] = max(matches.get(symbol, 0), len(name))
        if not matches:
            return ""
        longest = max(matches.values())
        candidates = sorted(symbol for symbol, size in matches.items() if size == longest)
        return candidates[0] if len(candidates) == 1 else ""

    def _run_llm_planner(self, plan: QueryPlan) -> tuple[QueryPlan, Dict[str, Any], str]:
        """用 LLM 補充資源規劃；失敗時保留 deterministic fallback。"""
        if not self.use_llm_planner or self.client is None or not self.client.enabled:
            return plan, {}, ""
        raw, info = gemini_call(
            "query_plan",
            client=self.client,
            root=self.root,
            metadata={
                "task": "query_plan",
                "query": plan.query,
                "trace_id": plan.trace_id,
            },
            query=plan.query,
            fallback_plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
        )
        payload = extract_json(raw or "")
        if not isinstance(payload, dict):
            return plan, info, "LLM planner 沒有回傳合法 JSON，已使用 deterministic fallback"
        return normalize_planner_payload(payload, fallback=plan), info, ""

    def _collect_evidence(self, plan: QueryPlan) -> EvidencePack:
        items: List[Dict[str, Any]] = []
        seen: set[str] = set()
        live_evidence_ids: set[str] = set()
        outcomes: List[Dict[str, Any]] = []
        allow_conversation = wants_conversation_context(plan)
        local_items, local_warnings, local_outcomes = (
            self._collect_local_ticker_evidence(plan)
        )
        for item in local_items:
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
        if local_items:
            outcomes.append({
                "resource": "canonical_local",
                "status": "ok",
                "live": False,
            })
        outcomes.extend(local_outcomes)
        search_terms = [plan.normalized_query, *plan.keywords]
        for term in search_terms:
            if not str(term).strip():
                continue
            try:
                evidence_rows = self.db.search_evidence(
                    str(term), limit=plan.max_sources,
                )
            except Exception:
                evidence_rows = []
            for row in evidence_rows:
                item = _row_to_evidence(row, root=self.root, db=self.db)
                if not _search_evidence_is_relevant(item, plan):
                    continue
                if is_conversation_evidence(item) and not allow_conversation:
                    continue
                if item["id"] not in seen:
                    seen.add(item["id"])
                    items.append(item)

            try:
                memory_rows = self.db.search_memory_rows(
                    str(term),
                    memory_layers=("short", "long"),
                    scope_type=plan.scope_type if plan.scope_type != "global" else None,
                    scope_id=plan.scope_id if plan.scope_id else None,
                    limit=plan.max_sources,
                )
            except Exception:
                memory_rows = []
            for memory_row in memory_rows:
                linked: List[EvidenceItemRow] = []
                try:
                    linked = self.db.list_memory_evidence(
                        memory_row.memory_id, limit=plan.max_sources,
                    )
                except Exception:
                    linked = []
                if memory_row.memory_type == "conversation_turn":
                    # 對話記憶可帶出已連結的官方／市場 evidence；其正文本身
                    # 只有在明確詢問歷史對話時才能進入 pack。
                    candidates = []
                    for row in linked:
                        linked_item = _row_to_evidence(
                            row, root=self.root, db=self.db,
                        )
                        if not is_conversation_evidence(linked_item):
                            candidates.append(linked_item)
                    if allow_conversation:
                        candidates.append(_memory_to_evidence(memory_row))
                else:
                    candidates = [
                        _row_to_evidence(row, root=self.root, db=self.db)
                        for row in linked
                    ]
                    if not candidates:
                        candidates = [_memory_to_evidence(memory_row)]
                for item in candidates:
                    if is_conversation_evidence(item) and not allow_conversation:
                        continue
                    if item["id"] not in seen:
                        seen.add(item["id"])
                        items.append(item)
            # 不在 live adapters 執行前因本地結果數量而停止；否則查詢同時要求
            # 基本面與重大訊息時，local ticker evidence 可能吃滿 quota，讓
            # news/MOPS 永遠不會進入 pack。最後再做「每個要求資源至少保留一筆」
            # 的公平選擇。

        if allow_conversation:
            try:
                recent_turns = self.db.list_memory_items(
                    memory_layer="short",
                    memory_type="conversation_turn",
                    memory_status="active",
                    limit=plan.max_sources,
                )
            except Exception:
                recent_turns = []
            for memory_row in recent_turns:
                if memory_row.quality_status not in {"ok", "partial"}:
                    continue
                item = _memory_to_evidence(memory_row)
                if item["id"] not in seen:
                    seen.add(item["id"])
                    items.append(item)

        live_resources = [
            resource for resource in plan.resource_needs
            if resource in {"news", "mops", "web_search", "professional_knowledge"}
        ]
        # ticker_snapshot has a valid local/cache path.  Only expose its live
        # adapter when the caller explicitly enabled live sources; otherwise a
        # normal local-first ask must not become ``live_disabled`` merely
        # because the plan also mentions a ticker snapshot.
        if self.enable_live_sources and "ticker_snapshot" in plan.resource_needs:
            live_resources.append("ticker_snapshot")
        live_allowed = (
            self.enable_live_sources
            and plan.freshness != "unbounded"
            and bool(live_resources)
        )
        live_results: List[ResourceResult] = []
        if live_allowed:
            unique_resources = list(dict.fromkeys(live_resources))
            executor = ThreadPoolExecutor(max_workers=len(unique_resources))
            futures = {
                resource: executor.submit(
                    (
                        MopsAdapter(
                            root=self.root,
                            timeout=max(
                                0.25,
                                min(8.0, self.live_budget_seconds),
                            ),
                        ).collect
                        if resource == "mops"
                        else LiveTickerAdapter(
                            root=self.root,
                            timeout=max(
                                0.25,
                                min(8.0, self.live_budget_seconds),
                            ),
                        ).collect
                        if resource == "ticker_snapshot"
                        else ProfessionalKnowledgeAdapter(
                            root=self.root,
                            timeout=max(
                                1.0,
                                min(8.0, self.live_budget_seconds),
                            ),
                        ).collect
                        if resource == "professional_knowledge"
                        else WebSearchAdapter(
                            root=self.root,
                            resource_name=resource,
                            timeout=max(1, int(min(8.0, self.live_budget_seconds))),
                        ).collect
                    ),
                    plan,
                )
                for resource in unique_resources
            }
            deadline = time.monotonic() + self.live_budget_seconds
            try:
                for resource, future in futures.items():
                    remaining = max(0.05, deadline - time.monotonic())
                    try:
                        live_results.append(future.result(timeout=remaining))
                    except FutureTimeout:
                        future.cancel()
                        live_results.append(ResourceResult(
                            resource=resource,
                            warnings=[
                                f"{resource} 超過 {self.live_budget_seconds:g}s 時間預算；不等於查無此事"
                            ],
                            status="timeout",
                            live=True,
                        ))
                    except Exception as exc:
                        live_results.append(ResourceResult(
                            resource=resource,
                            warnings=[f"{resource} 執行失敗：{type(exc).__name__}: {exc}"],
                            status="failed",
                            live=True,
                        ))
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        elif live_resources:
            for resource in list(dict.fromkeys(live_resources)):
                live_results.append(ResourceResult(
                    resource=resource,
                    warnings=[
                        f"{resource} 即時來源未啟用；可設定 STOCK_ASK_LIVE=1 "
                        "或 enable_live_sources=True；不等於查無此事"
                    ],
                    status="live_disabled",
                ))
        for live_result in live_results:
            outcome = {
                "resource": live_result.resource,
                "status": (
                    "resource_failed" if live_result.status == "failed"
                    else live_result.status
                ),
                "live": bool(live_result.live),
                "warnings": list(live_result.warnings),
            }
            if live_result.metadata:
                outcome["metadata"] = dict(live_result.metadata)
            outcomes.append(outcome)
            for item in live_result.evidence:
                item_id = str(item.get("id") or "")
                if item_id:
                    live_evidence_ids.add(item_id)
                if item_id not in seen:
                    seen.add(item["id"])
                    items.append(item)

        selected_items: List[Dict[str, Any]] = []
        selected_ids: set[str] = set()
        # 先滿足使用者明確要求的非 memory 資源，再用剩餘 quota 補充相關
        # evidence。這避免大量本地快取／記憶把即時新聞或 MOPS 擠掉。
        for resource in plan.resource_needs:
            if resource == "memory":
                continue
            candidates = [
                item for item in items
                if (
                    str(item.get("id") or "")
                    and _evidence_matches_resource(item, resource)
                )
            ]
            # Local cache is inserted first for deterministic fallback, but a
            # successful live collection must win the same-resource slot so a
            # stale local row cannot crowd out a fresh quote/news item.
            candidates.sort(
                key=lambda item: str(item.get("id") or "") in live_evidence_ids,
                reverse=True,
            )
            for item in candidates:
                item_id = str(item.get("id") or "")
                if item_id and item_id not in selected_ids:
                    selected_ids.add(item_id)
                    selected_items.append(item)
                    break
        for item in items:
            if len(selected_items) >= plan.max_sources:
                break
            item_id = str(item.get("id") or "")
            if item_id and item_id not in selected_ids:
                selected_ids.add(item_id)
                selected_items.append(item)

        pack = build_evidence_pack(
            plan.query,
            plan_id=plan.plan_id,
            trace_id=plan.trace_id,
            items=selected_items[:plan.max_sources],
        )
        try:
            persist_evidence_items(self.db, pack.items)
        except Exception:
            pack.warnings.append("evidence registry 寫入失敗；本次回答仍以暫存證據驗證")
        stale_count = 0
        temporal_rejected_count = 0
        temporal_mixed_count = 0
        temporal_policy = getattr(plan, "temporal_policy", "warn_mixed")
        for item in pack.items:
            if item.get("quality_status") not in {"ok", "partial"}:
                continue
            if is_evidence_fresh(item, plan.freshness):
                if not is_evidence_temporally_acceptable(item, temporal_policy):
                    item["quality_status"] = "invalid"
                    temporal_rejected_count += 1
                elif (
                    str(temporal_policy or "").strip().lower() == "warn_mixed"
                    and evidence_temporal_status(item) == "mixed"
                ):
                    temporal_mixed_count += 1
                continue
            item["quality_status"] = "stale"
            stale_count += 1

        available_kinds = {
            str(item.get("kind") or "") for item in pack.usable_items
            if not is_conversation_evidence(item)
        }
        if allow_conversation:
            available_kinds.update(
                str(item.get("kind") or "") for item in pack.usable_items
                if is_conversation_evidence(item)
            )
        missing: List[str] = []
        for resource in plan.resource_needs:
            if resource == "memory":
                continue
            if not any(_evidence_matches_resource(item, resource) for item in pack.usable_items):
                missing.append(resource)
        warnings = []
        warnings.extend(local_warnings)
        for live_result in live_results:
            warnings.extend(live_result.warnings)
        if stale_count:
            warnings.append(
                f"{stale_count} 筆證據未符合 freshness={plan.freshness}，已排除出回答上下文"
            )
        if temporal_rejected_count:
            warnings.append(
                f"{temporal_rejected_count} 筆證據未符合 temporal_policy，已排除出回答上下文"
            )
        if temporal_mixed_count:
            warnings.append(
                f"{temporal_mixed_count} 筆證據 temporal_status=mixed（跨日），已保留並警告"
            )
        if plan.planner_required:
            warnings.append("此問題需要 LLM planner 補充資源判斷；目前先使用安全 fallback")
        if missing:
            warnings.append("尚未接入或尚未取得資源：" + ", ".join(missing))
        pack.missing_resources = missing
        pack.warnings.extend(warnings)
        pack.resource_outcomes = outcomes
        pack.collection_status = derive_collection_status(
            outcomes,
            missing_resources=missing,
            usable_count=len(pack.usable_items),
            item_count=len(pack.items),
        )
        return pack

    def _collect_local_ticker_evidence(
        self, plan: QueryPlan,
    ) -> tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
        """把已存在於 canonical SQLite tables 的標的資料轉成 evidence。

        這個 adapter 僅讀本地資料，不在問答流程中偷偷觸發外部 API；來源缺失
        時由上層的 ``missing_resources`` 與 abstain policy 處理。
        本地 adapter 失敗會以明確 ``resource_outcomes`` 回報，不靠警告文字比對。
        """
        if not plan.ticker:
            # TAIFEX 是市場級資源，不需要個股 ticker；保留它自己的 exact
            # evidence 路徑，不能因 ticker 空白而整條需求被跳過。
            if "taifex" in plan.resource_needs:
                try:
                    from bot.ask_adapters import collect_snapshot_resource_evidence

                    return collect_snapshot_resource_evidence(
                        "",
                        root=self.root,
                        resource_needs=["taifex"],
                    ), [], []
                except Exception as exc:
                    return [], [f"TAIFEX local adapter 失敗：{type(exc).__name__}: {exc}"], []
            return [], [], []
        items: List[Dict[str, Any]] = []
        warnings: List[str] = []
        outcomes: List[Dict[str, Any]] = []
        info = self.db.get_stock_info(plan.ticker)
        if info and info.quality_status in {"ok", "partial"}:
            items.append(make_evidence(
                "ticker_snapshot",
                source=info.source_record_id or "stock_info",
                source_ref=info.source_record_id or info.symbol,
                title=f"{info.symbol} 公司基本資料",
                url=info.source_url,
                observed_at=info.source_date or info.retrieved_at or info.updated_at,
                excerpt=(
                    f"{info.symbol} {info.name}，市場 {info.market}，"
                    f"產業 {info.industry}"
                ),
                quality_status=info.quality_status,
                source_document_id=_registered_source_document_id(
                    self.db, self.root, info.source_record_id,
                ),
            ))

        bars = self.db.get_price_history(plan.ticker, limit=1, ascending=False)
        if bars:
            bar = bars[0]
            if bar.quality_status in {"ok", "partial"}:
                bar_is_normalized_cache = str(bar.source or "").lower() == "yfinance"
                items.append(make_evidence(
                    "ticker_snapshot",
                    source=bar.source_record_id or bar.source,
                    source_ref=bar.source_record_id or f"{bar.symbol}:{bar.date}",
                    title=f"{bar.symbol} 日 K {bar.date}",
                    url=bar.source_url,
                    observed_at=bar.source_date or bar.date,
                    excerpt=(
                        f"{bar.symbol} {bar.date} 開 {bar.open:g} 高 {bar.high:g} "
                        f"低 {bar.low:g} 收 {bar.close:g} 量 {bar.volume:g}"
                    ),
                    quality_status=bar.quality_status,
                    source_document_id=_registered_source_document_id(
                        self.db, self.root, bar.source_record_id,
                    ),
                    provenance_status=(
                        "cache_only" if bar_is_normalized_cache else ""
                    ),
                    metadata=(
                        {"cache_only": True, "normalized": True}
                        if bar_is_normalized_cache else {}
                    ),
                ))

        if "fundamental" in plan.resource_needs:
            revenue_result = MonthlyRevenueAdapter(root=self.root).collect(plan)
            items.extend(revenue_result.evidence)
            warnings.extend(revenue_result.warnings)
            if revenue_result.status == "failed":
                failure_warnings = list(revenue_result.warnings)
                failure_warnings.append("月營收來源失敗；不等於查無此事")
                warnings.append("月營收來源失敗；不等於查無此事")
                outcomes.append({
                    "resource": revenue_result.resource or "fundamental",
                    "status": "resource_failed",
                    "live": False,
                    "warnings": failure_warnings,
                    "metadata": {"adapter": "monthly_revenue"},
                })

            valuations = self.db.get_valuation(plan.ticker)
            valuation = next(
                (row for row in valuations if row.quality_status in {"ok", "partial"}),
                None,
            )
            if valuation:
                valuation_source_id = _registered_source_document_id(
                    self.db, self.root, valuation.source_record_id,
                )
                items.append(make_evidence(
                    "valuation_daily",
                    source=valuation.source_record_id or "valuation_daily",
                    source_ref=valuation.source_record_id or f"{valuation.symbol}:{valuation.as_of}",
                    title=f"{valuation.symbol} 估值 {valuation.as_of}",
                    url=valuation.source_url,
                    observed_at=valuation.source_date or valuation.as_of,
                    as_of=valuation.as_of,
                    retrieved_at=valuation.retrieved_at,
                    excerpt=(
                        f"{valuation.symbol} {valuation.as_of} PER {valuation.pe_ratio:g}、"
                        f"PBR {valuation.pb_ratio:g}、殖利率 {valuation.dividend_yield:g}%"
                    ),
                    quality_status=valuation.quality_status,
                    source_document_id=valuation_source_id,
                    provenance_status="exact" if valuation_source_id else "unresolved",
                ))

            dividends = [
                row for row in self.db.get_dividend_history(plan.ticker)
                if row.quality_status in {"ok", "partial"}
            ]
            # 「近年／歷年」問題需要保留多個年度，不能只取最新一筆；一般
            # 基本面問題則維持單筆，避免佔滿 EvidencePack 的配額。
            dividend_rows = (
                dividends[:5]
                if any(word in plan.normalized_query for word in (
                    "近年", "歷年", "歷來", "近幾年", "各年度",
                ))
                else dividends[:1]
            )
            for dividend in dividend_rows:
                dividend_source_id = _registered_source_document_id(
                    self.db, self.root, dividend.source_record_id,
                )
                items.append(make_evidence(
                    "dividend_history",
                    source=dividend.source_record_id or "dividend_history",
                    source_ref=dividend.source_record_id or f"{dividend.symbol}:{dividend.year}",
                    title=f"{dividend.symbol} 股利 {dividend.year}",
                    url=dividend.source_url,
                    observed_at=dividend.source_date or str(dividend.year),
                    as_of=str(dividend.year),
                    retrieved_at=dividend.retrieved_at,
                    excerpt=(
                        f"{dividend.symbol} {dividend.year} 現金股利 {dividend.cash_dividend:g}、"
                        f"股票股利 {dividend.stock_dividend:g}"
                    ),
                    quality_status=dividend.quality_status,
                    source_document_id=dividend_source_id,
                    provenance_status="exact" if dividend_source_id else "unresolved",
                ))

            quarterlies = self.db.get_quarterly_report(plan.ticker)
            if quarterlies:
                quarterly = next(
                    (row for row in quarterlies if row.quality_status in {"ok", "partial"}),
                    None,
                )
                if quarterly:
                    # 季報已有 canonical/research slice；舊快取若沒有 exact
                    # source_document_id 仍維持 unresolved，不臆造來源。
                    quarterly_source_id = _registered_source_document_id(
                        self.db, self.root, quarterly.source_record_id,
                    )
                    items.append(make_evidence(
                        "fundamental",
                        source=quarterly.source_record_id or "quarterly_report",
                        source_ref=quarterly.source_record_id or f"{quarterly.symbol}:{quarterly.period}",
                        title=f"{quarterly.symbol} 季報 {quarterly.period}",
                        url=quarterly.source_url,
                        observed_at=quarterly.source_date or quarterly.period,
                        excerpt=(
                            f"{quarterly.symbol} {quarterly.period} EPS {quarterly.eps:g}，"
                            f"毛利率 {quarterly.gross_margin:g}%、營業利益率 "
                            f"{quarterly.op_margin:g}%、淨利率 {quarterly.net_margin:g}%"
                        ),
                        # 使用 canonical 列的 retrieved_at；缺值保持空白，
                        # 不可把 ask 當下時間當成舊快取列的擷取證據。
                        retrieved_at=str(quarterly.retrieved_at or ""),
                        quality_status=quarterly.quality_status,
                        provenance_status=(
                            "exact" if quarterly_source_id else "unresolved"
                        ),
                        source_document_id=quarterly_source_id,
                        metadata={"cache_only": not bool(quarterly.source_record_id)},
                    ))

        # 本地 snapshot adapters（技術／籌碼／ETF／macro／基本面快取）
        try:
            from bot.ask_adapters import collect_snapshot_resource_evidence

            items.extend(
                collect_snapshot_resource_evidence(
                    plan.ticker,
                    root=self.root,
                    resource_needs=list(plan.resource_needs),
                )
            )
        except Exception:
            warnings.append("snapshot adapter 發生例外，該來源未納入本次回答")
            outcomes.append({
                "resource": "snapshot",
                "status": "resource_failed",
                "live": False,
            })
        return items, warnings, outcomes

    def _remember_turn(
        self,
        plan: QueryPlan,
        result: AskResult,
    ) -> None:
        """保存完整問答到短期記憶；不論回答成功與否都不進長期層。

        conversation_turn 即使驗證通過也只是對話稽核，不可自動升格為市場事實。
        """
        try:
            quality = "partial" if result.verified else "unknown"
            self.memory.put_short(
                json.dumps({
                    "query": result.query,
                    "answer": result.answer,
                    "status": result.status,
                    "plan": plan.to_dict(),
                    "verification": result.verification.to_dict(),
                }, ensure_ascii=False, sort_keys=True),
                memory_id=f"ask:{result.trace_id}",
                memory_type="conversation_turn",
                scope_type=plan.scope_type,
                scope_id=plan.scope_id,
                subject=plan.ticker,
                summary=result.answer[:1200],
                keywords=plan.keywords,
                importance=0.45 if result.verified else 0.1,
                confidence=0.8 if result.verified else 0.0,
                quality_status=quality,
                source="ask",
                source_ref=result.trace_id,
                source_record_id=result.trace_id,
                evidence=[
                    item for item in result.evidence.items
                    if not is_conversation_evidence(item)
                ],
                metadata={
                    "status": result.status,
                    "plan_id": plan.plan_id,
                    "collection_status": result.evidence.collection_status,
                    "not_market_fact": True,
                },
                observed_at=now_tw().isoformat(timespec="seconds"),
                trace_id=result.trace_id,
            )
        except Exception:
            # 問答不能因記憶鏡像失敗而中斷；LLM log 與回答仍可回傳。
            return

    def _persist_verified_answer(
        self,
        plan: QueryPlan,
        pack: EvidencePack,
        payload: Any,
        verification: VerificationResult,
    ) -> None:
        """把通過驗證的回答、claims 與來源關聯寫入 relational layer。"""
        if not isinstance(payload, dict) or not verification.valid:
            return
        now = now_tw().isoformat(timespec="seconds")
        run_id = plan.trace_id
        self.db.upsert_llm_run(LlmRunRow(
            run_id=run_id,
            task_type="ask_answer",
            model=getattr(self.client, "model", "") if self.client else "",
            prompt_id="ask_answer",
            prompt_version="1.1",
            started_at=now,
            finished_at=now,
            status="verified",
            input_hash=hashlib.sha256(plan.query.encode("utf-8")).hexdigest(),
            output_hash=hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            trace_id=plan.trace_id,
            metadata_json=json.dumps({"plan_id": plan.plan_id}, ensure_ascii=False),
        ))
        for ordinal, evidence_id in enumerate(verification.cited_evidence_ids):
            try:
                self.db.link_llm_run_evidence(LlmRunEvidenceRow(
                    run_id=run_id, evidence_id=evidence_id, ordinal=ordinal,
                ))
            except Exception:
                continue
        self.db.upsert_llm_output(LlmOutputRow(
            output_id=f"{run_id}:output",
            run_id=run_id,
            output_format="json",
            structured_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            markdown=str(payload.get("answer") or "")[:20_000],
            confidence=float(payload.get("confidence") or 0.0),
            limitation=str(payload.get("uncertainty") or "")[:4000],
            is_publishable=1,
        ))
        claims = payload.get("claims") if isinstance(payload.get("claims"), list) else []
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                continue
            claim_id = f"{run_id}:claim:{index}"
            try:
                claim_confidence = float(
                    claim.get("confidence") or payload.get("confidence") or 0.0
                )
            except (TypeError, ValueError):
                claim_confidence = 0.0
            claim_evidence_ids = claim.get("evidence_ids")
            if not isinstance(claim_evidence_ids, list):
                claim_evidence_ids = verification.cited_evidence_ids
            self.db.upsert_claim(ClaimRow(
                claim_id=claim_id,
                run_id=run_id,
                text=str(claim.get("text") or claim.get("statement") or ""),
                claim_type=str(claim.get("type") or "fact"),
                confidence=claim_confidence,
                status="verified",
                uncertainty=str(claim.get("uncertainty") or ""),
                created_at=now,
                updated_at=now,
            ))
            for ordinal, evidence_id in enumerate(claim_evidence_ids):
                try:
                    self.db.link_claim_evidence(ClaimEvidenceRow(
                        claim_id=claim_id,
                        evidence_id=str(evidence_id),
                        ordinal=ordinal,
                    ))
                except Exception:
                    continue

    def follow_up(self, trace_id: str) -> EvidencePack:
        """沿用原 trace 與 QueryPlan，再收集待取得來源。"""
        row = self.db.get_ask_run(str(trace_id or ""))
        if not row:
            raise ValueError(f"找不到 ask trace: {trace_id}")
        try:
            plan_data = json.loads(row.get("plan_json") or "{}")
            plan = QueryPlan(**plan_data)
        except Exception as exc:
            raise ValueError(f"ask trace plan 無法解析: {exc}") from exc
        if plan.trace_id != trace_id:
            raise ValueError("ask trace_id 與 plan 不一致")
        pack = self._collect_evidence(plan)
        follow_status = pack.collection_status
        if follow_status == "ok":
            follow_status = "partial" if pack.missing_resources else "collected"
        self.db.upsert_ask_run(
            plan.trace_id,
            query=plan.query,
            plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
            evidence_json=json.dumps(pack.to_dict(), ensure_ascii=False),
            pending_resources=json.dumps(pack.missing_resources, ensure_ascii=False),
            status=follow_status,
        )
        return pack

    def ask(
        self,
        query: str,
        *,
        ticker: str = "",
        scope_type: str = "global",
        scope_id: str = "",
    ) -> AskResult:
        detected_ticker = str(ticker or "").strip() or self._infer_ticker_from_local_names(query)
        plan = plan_query(
            query,
            ticker=detected_ticker,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        plan, planner_info, planner_warning = self._run_llm_planner(plan)
        pack = self._collect_evidence(plan)
        if planner_warning:
            pack.warnings.append(planner_warning)
        verification = VerificationResult(
            valid=False,
            errors=["尚未執行 LLM 回答驗證"],
        )
        llm_info: Dict[str, Any] = {}
        if planner_info:
            llm_info["query_plan"] = planner_info
        answer = UNCERTAIN_ANSWER
        payload: Any = None
        verification_attempted = False
        answered = False
        required_missing = [
            resource for resource in pack.missing_resources
            if resource not in {
                "ticker_snapshot",
                "historical_evidence",
                "channel_knowledge",
                "user_memory",
            }
        ]
        market_usable = pack.market_usable_items
        conversation_usable = [
            item for item in pack.usable_items if is_conversation_evidence(item)
        ]
        has_answer_context = bool(
            market_usable
            or (wants_conversation_context(plan) and conversation_usable)
        )

        if required_missing:
            verification = VerificationResult(
                valid=False,
                errors=["尚未取得必要資源: " + ", ".join(required_missing)],
            )
        elif not has_answer_context:
            verification = VerificationResult(
                valid=False,
                errors=["沒有可用且品質合格的 evidence"],
            )
        elif self.client is None or not self.client.enabled:
            verification = VerificationResult(
                valid=False,
                errors=["LLM 未啟用，無法產生受證據約束的回答"],
            )
        else:
            verification_attempted = True
            prompt_evidence_ids = [
                str(item["id"]) for item in pack.usable_items if item.get("id")
            ]
            try:
                raw, answer_info = gemini_call(
                    "ask_answer",
                    client=self.client,
                    root=self.root,
                    metadata={
                        "task": "ask_answer",
                        "query": plan.query,
                        "trace_id": plan.trace_id,
                        "evidence_ids": prompt_evidence_ids,
                    },
                    query=plan.query,
                    plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
                    evidence_text=pack.to_prompt_text(),
                    missing_resources=", ".join(pack.missing_resources) or "(無)",
                )
            except Exception as exc:
                raw, answer_info = "", {"error": f"{type(exc).__name__}: {exc}"}
            llm_info["answer"] = answer_info
            payload = extract_json(raw or "")
            verification = verify_answer_payload(payload, pack)
            if not verification.valid:
                try:
                    repair_raw, repair_info = gemini_call(
                        "ask_answer",
                        client=self.client,
                        root=self.root,
                        metadata={
                            "task": "ask_answer_repair",
                            "query": plan.query,
                            "trace_id": plan.trace_id,
                            "evidence_ids": prompt_evidence_ids,
                        },
                        query=plan.query,
                        plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
                        evidence_text=pack.to_prompt_text(),
                        previous_answer_json=json.dumps(
                            payload or {}, ensure_ascii=False,
                        ),
                        repair_feedback="; ".join(verification.errors),
                        missing_resources=", ".join(pack.missing_resources) or "(無)",
                    )
                    repair_payload = extract_json(repair_raw or "")
                    repair_verification = verify_answer_payload(repair_payload, pack)
                    llm_info["answer_repair"] = repair_info
                    if repair_verification.valid:
                        payload = repair_payload
                        verification = repair_verification
                except Exception as exc:
                    llm_info["answer_repair"] = {
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            if verification.valid and isinstance(payload, dict):
                answer = str(payload.get("answer") or UNCERTAIN_ANSWER).strip()
                answered = True

        status = derive_ask_status(
            collection_status=pack.collection_status,
            has_usable=has_answer_context,
            has_items=bool(pack.items),
            required_missing=required_missing,
            llm_enabled=bool(self.client is not None and self.client.enabled),
            verification=verification,
            answer=answer,
            answered=answered,
            verification_attempted=verification_attempted,
        )

        result = AskResult(
            query=plan.query,
            answer=answer,
            status=status,
            trace_id=plan.trace_id,
            plan=plan,
            evidence=pack,
            verification=verification,
            llm_info=llm_info,
            pending_resources=list(pack.missing_resources),
        )
        if result.verified:
            try:
                self._persist_verified_answer(plan, pack, payload, verification)
            except Exception as exc:
                result.llm_info["research_persistence_warning"] = (
                    f"verified answer 寫入失敗：{type(exc).__name__}: {exc}"
                )
        try:
            self.db.upsert_ask_run(
                plan.trace_id,
                query=plan.query,
                plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
                evidence_json=json.dumps(pack.to_dict(), ensure_ascii=False),
                pending_resources=json.dumps(pack.missing_resources, ensure_ascii=False),
                status=status,
            )
        except Exception as exc:
            result.llm_info["ask_trace_persistence_warning"] = (
                f"ask trace 寫入失敗：{type(exc).__name__}: {exc}"
            )
        self._remember_turn(plan, result)
        return result


__all__ = [
    "ASK_STATUSES",
    "AskEngine",
    "AskResult",
    "UNCERTAIN_ANSWER",
    "derive_ask_status",
    "derive_collection_status",
    "wants_conversation_context",
]

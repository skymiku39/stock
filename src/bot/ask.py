"""本地優先的 ``!ask`` 問答垂直切片。

接通 QueryPlan、SQLite memory/evidence、本地 snapshot adapters（技術／籌碼／
ETF／macro／基本面）與 Gemini answer。Ask 路徑不偷偷打外網；來源缺失時回傳
不確定，並只保存短期稽核記錄，不把答案升級成長期記憶。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.answer_verifier import VerificationResult, verify_answer_payload
from bot.evidence import (
    EvidencePack,
    build_evidence_pack,
    is_evidence_fresh,
    make_evidence,
)
from bot.llm_analyzer import GeminiClient, extract_json, gemini_call
from bot.memory_store import MemoryStore
from bot.query_plan import QueryPlan, normalize_planner_payload, plan_query
from bot.stock_db import EvidenceItemRow, MemoryItemRow, StockDB
from bot.utils import now_tw


UNCERTAIN_ANSWER = "我不確定，目前沒有足夠的可驗證資料。"


@dataclass
class AskResult:
    query: str
    answer: str
    status: str  # answered / uncertain / no_evidence / verification_failed
    trace_id: str
    plan: QueryPlan
    evidence: EvidencePack
    verification: VerificationResult
    llm_info: Dict[str, Any] = field(default_factory=dict)

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
        }


def _row_to_evidence(row: EvidenceItemRow) -> Dict[str, Any]:
    return {
        "id": row.evidence_id,
        "kind": row.kind,
        "source": row.source,
        "source_ref": row.source_ref,
        "title": row.title,
        "url": row.url,
        "observed_at": row.observed_at,
        "excerpt": row.excerpt,
        "quality_status": row.quality_status,
    }


def _memory_to_evidence(row: MemoryItemRow) -> Dict[str, Any]:
    return make_evidence(
        "memory",
        source=row.source or "memory_store",
        source_ref=row.source_record_id or row.memory_id,
        title=row.summary or row.subject or row.memory_id,
        observed_at=row.observed_at or row.updated_at,
        excerpt=row.content or row.summary,
        quality_status=row.quality_status,
    )


class AskEngine:
    """以 SQLite 為真實來源的問答執行器。"""

    def __init__(
        self,
        *,
        root: Path,
        db: Optional[StockDB] = None,
        client: Optional[GeminiClient] = None,
        use_llm_planner: bool = True,
    ) -> None:
        self.root = Path(root)
        self.db = db or StockDB.open(root=self.root)
        self.client = client
        self.use_llm_planner = use_llm_planner
        self.memory = MemoryStore(self.db, actor="ask")

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
        for item in self._collect_local_ticker_evidence(plan):
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
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
                item = _row_to_evidence(row)
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
                candidates = [_row_to_evidence(row) for row in linked]
                if not candidates:
                    candidates = [_memory_to_evidence(memory_row)]
                for item in candidates:
                    if item["id"] not in seen:
                        seen.add(item["id"])
                        items.append(item)
            if len(items) >= plan.max_sources:
                break

        pack = build_evidence_pack(
            plan.query,
            plan_id=plan.plan_id,
            trace_id=plan.trace_id,
            items=items[:plan.max_sources],
        )
        stale_count = 0
        for item in pack.items:
            if item.get("quality_status") not in {"ok", "partial"}:
                continue
            if is_evidence_fresh(item, plan.freshness):
                continue
            item["quality_status"] = "stale"
            stale_count += 1

        available_kinds = {
            str(item.get("kind") or "") for item in pack.usable_items
        }
        kind_map = {
            "historical_evidence": {"memory", "history", "historical"},
            "channel_knowledge": {"memory", "channel"},
            "user_memory": {"memory", "user", "preference"},
        }
        missing: List[str] = []
        for resource in plan.resource_needs:
            if resource == "memory":
                continue
            expected = kind_map.get(resource, {resource})
            if not available_kinds.intersection(expected):
                missing.append(resource)
        warnings = []
        if stale_count:
            warnings.append(
                f"{stale_count} 筆證據未符合 freshness={plan.freshness}，已排除出回答上下文"
            )
        if plan.planner_required:
            warnings.append("此問題需要 LLM planner 補充資源判斷；目前先使用安全 fallback")
        if missing:
            warnings.append("尚未接入或尚未取得資源：" + ", ".join(missing))
        pack.missing_resources = missing
        pack.warnings.extend(warnings)
        return pack

    def _collect_local_ticker_evidence(self, plan: QueryPlan) -> List[Dict[str, Any]]:
        """把已存在於 canonical SQLite tables 的標的資料轉成 evidence。

        這個 adapter 僅讀本地資料，不在問答流程中偷偷觸發外部 API；來源缺失
        時由上層的 ``missing_resources`` 與 abstain policy 處理。
        """
        if not plan.ticker:
            return []
        items: List[Dict[str, Any]] = []
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
            ))

        bars = self.db.get_price_history(plan.ticker, limit=1, ascending=False)
        if bars:
            bar = bars[0]
            if bar.quality_status in {"ok", "partial"}:
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
                ))

        if "fundamental" in plan.resource_needs:
            revenues = self.db.get_monthly_revenue(plan.ticker)
            if revenues:
                revenue = next(
                    (row for row in revenues if row.quality_status in {"ok", "partial"}),
                    None,
                )
                if revenue:
                    items.append(make_evidence(
                        "fundamental",
                        source=revenue.source_record_id or "monthly_revenue",
                        source_ref=revenue.source_record_id or f"{revenue.symbol}:{revenue.year_month}",
                        title=f"{revenue.symbol} 月營收 {revenue.year_month}",
                        url=revenue.source_url,
                        observed_at=revenue.source_date or revenue.year_month,
                        excerpt=(
                            f"{revenue.symbol} {revenue.year_month} 月營收 {revenue.revenue:g}，"
                            f"YoY {revenue.yoy_pct:+g}%、MoM {revenue.mom_pct:+g}%"
                        ),
                        quality_status=revenue.quality_status,
                    ))

            quarterlies = self.db.get_quarterly_report(plan.ticker)
            if quarterlies:
                quarterly = next(
                    (row for row in quarterlies if row.quality_status in {"ok", "partial"}),
                    None,
                )
                if quarterly:
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
                        quality_status=quarterly.quality_status,
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
            pass
        return items

    def _remember_turn(
        self,
        plan: QueryPlan,
        result: AskResult,
    ) -> None:
        """保存完整問答到短期記憶；不論回答成功與否都不進長期層。"""
        try:
            quality = "ok" if result.verified else "unknown"
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
                evidence=result.evidence.items,
                metadata={"status": result.status, "plan_id": plan.plan_id},
                observed_at=now_tw().isoformat(timespec="seconds"),
                trace_id=result.trace_id,
            )
        except Exception:
            # 問答不能因記憶鏡像失敗而中斷；LLM log 與回答仍可回傳。
            return

    def ask(
        self,
        query: str,
        *,
        ticker: str = "",
        scope_type: str = "global",
        scope_id: str = "",
    ) -> AskResult:
        plan = plan_query(
            query,
            ticker=ticker,
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
        status = "no_evidence"

        if not pack.usable_items:
            verification = VerificationResult(
                valid=False,
                errors=["沒有可用且品質合格的 evidence"],
            )
        elif self.client is None or not self.client.enabled:
            verification = VerificationResult(
                valid=False,
                errors=["LLM 未啟用，無法產生受證據約束的回答"],
            )
            status = "uncertain"
        else:
            raw, answer_info = gemini_call(
                "ask_answer",
                client=self.client,
                root=self.root,
                metadata={
                    "task": "ask_answer",
                    "query": plan.query,
                    "trace_id": plan.trace_id,
                },
                query=plan.query,
                plan_json=json.dumps(plan.to_dict(), ensure_ascii=False),
                evidence_text=pack.to_prompt_text(),
                missing_resources=", ".join(pack.missing_resources) or "(無)",
            )
            llm_info["answer"] = answer_info
            payload = extract_json(raw or "")
            verification = verify_answer_payload(payload, pack)
            if verification.valid and isinstance(payload, dict):
                answer = str(payload.get("answer") or UNCERTAIN_ANSWER).strip()
                status = "uncertain" if answer.startswith("我不確定") else "answered"
            else:
                status = "verification_failed"

        result = AskResult(
            query=plan.query,
            answer=answer,
            status=status,
            trace_id=plan.trace_id,
            plan=plan,
            evidence=pack,
            verification=verification,
            llm_info=llm_info,
        )
        self._remember_turn(plan, result)
        return result


__all__ = ["AskEngine", "AskResult", "UNCERTAIN_ANSWER"]

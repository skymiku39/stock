"""可追溯的研究證據項目。

LLM 可以使用摘要與原文，但下游保存時應保留穩定的 evidence id，避免只有
「新聞」「網路搜尋」這種無法回查的來源標籤。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping

from bot.utils import TW_TZ, now_tw


FRESHNESS_WINDOWS = {
    "24h": dt.timedelta(hours=24),
    "7d": dt.timedelta(days=7),
    "30d": dt.timedelta(days=30),
}
# 財報與營收的 observed_at 通常代表「資料所屬期間」，不是抓取時間。
# 技術面 last_date、籌碼彙總日、macro asof 亦同：在 schema 尚未分離
# as_of / retrieved_at 前，不可因超過 24h 就誤判不可用。
# quarterly_report／valuation_daily／dividend_history 依 ASK／Evidence
# 契約以 retrieved_at 判斷 freshness；估值／股利嚴格窗口不 fallback。
PERIOD_BASED_KINDS = {
    "fundamental",
    "monthly_revenue",
    "quarterly_report",
    "valuation_daily",
    "dividend_history",
    "technical",
    "chips",
    "etf",
    "macro",
}
_RETRIEVAL_ONLY_PERIOD_KINDS = frozenset({"valuation_daily", "dividend_history"})

# quality_status = 內容品質；provenance_status = 能否回到 exact raw document。
# 兩者必須分離：缺少 source_document_id 不代表資料內容錯誤。
PROVENANCE_STATUSES = {
    "exact",
    "composite_exact",
    "cache_only",
    "unresolved",
    "legacy_unlinked",
}
CONVERSATION_EVIDENCE_KINDS = {"conversation", "conversation_turn"}


def evidence_id(kind: str, *, source_ref: str = "", title: str = "", text: str = "") -> str:
    seed = "|".join((kind, source_ref, title, text[:500]))
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


def _has_source_document_identity(
    source_document_id: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    """是否具備可宣告的 source-document 身分（不驗證 raw 可否回放）。"""
    if str(source_document_id or "").strip():
        return True
    linked = (metadata or {}).get("source_document_ids")
    if isinstance(linked, list):
        return any(str(value).strip() for value in linked)
    return False


def resolve_provenance_status(
    *,
    source_document_id: str = "",
    provenance_status: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """推導 provenance；永不依 URL／標題臆造 source_document_id。

    ``exact``／``composite_exact`` 需要有效的 source-document 身分。純函式無法
    證明 raw 可回放，因此缺少身分時不得保留這兩個標籤，改保留
    ``cache_only``／``unresolved``／``legacy_unlinked`` 等保守狀態。
    """
    meta = dict(metadata or {})
    explicit = str(
        provenance_status or meta.get("provenance_status") or ""
    ).strip().lower()
    has_identity = _has_source_document_identity(source_document_id, meta)
    if explicit in {"exact", "composite_exact"} and not has_identity:
        # 明確標籤缺少身分時不得倖存；落到下方 cache_only／unresolved 判斷。
        explicit = ""
    elif explicit in PROVENANCE_STATUSES:
        return explicit
    if str(source_document_id or "").strip():
        return "exact"
    if meta.get("cache_only") is True or str(meta.get("cache_only") or "").lower() in {
        "1", "true", "yes",
    }:
        return "cache_only"
    if str(meta.get("legacy") or "").lower() in {"1", "true", "yes", "unlinked"}:
        return "legacy_unlinked"
    return "unresolved"


def is_conversation_evidence(item: Mapping[str, Any]) -> bool:
    """判斷是否為對話上下文，而非可核對的市場／官方事實來源。"""
    kind = str(item.get("kind") or "").strip().lower()
    if kind in CONVERSATION_EVIDENCE_KINDS:
        return True
    metadata = item.get("metadata")
    if isinstance(metadata, Mapping):
        if metadata.get("not_market_fact") is True:
            return True
        if str(metadata.get("memory_type") or "").strip() == "conversation_turn":
            return True
    return False


def make_evidence(
    kind: str,
    *,
    source: str,
    source_ref: str = "",
    title: str = "",
    url: str = "",
    observed_at: str = "",
    as_of: str = "",
    published_at: str = "",
    retrieved_at: str = "",
    valid_from: str = "",
    valid_to: str = "",
    excerpt: str = "",
    quality_status: str = "ok",
    source_document_id: str = "",
    provenance_status: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    title = str(title or "").strip()
    excerpt = str(excerpt or "").strip()
    meta = dict(metadata or {})
    document_id = str(source_document_id or "").strip()
    provenance = resolve_provenance_status(
        source_document_id=document_id,
        provenance_status=provenance_status,
        metadata=meta,
    )
    meta["provenance_status"] = provenance
    if provenance == "cache_only":
        meta["cache_only"] = True
    return {
        "id": evidence_id(kind, source_ref=source_ref, title=title, text=excerpt),
        "kind": str(kind or "unknown"),
        "source": str(source or "unknown"),
        "source_ref": str(source_ref or ""),
        "title": title[:300],
        "url": str(url or ""),
        "observed_at": str(observed_at or ""),
        "as_of": str(as_of or ""),
        "published_at": str(published_at or ""),
        "retrieved_at": str(retrieved_at or ""),
        "valid_from": str(valid_from or ""),
        "valid_to": str(valid_to or ""),
        "excerpt": excerpt[:2000],
        "quality_status": str(quality_status or "unknown"),
        # This is an explicit identity supplied by the collector.  It is
        # intentionally never inferred from URL/title/text.
        "source_document_id": document_id,
        "provenance_status": provenance,
        "metadata": meta,
    }


def normalize_evidence_packet(packet: Mapping[str, Any]) -> Dict[str, Any]:
    """將來源資料限制成可送入 LLM 與 SQLite registry 的最小契約。"""
    raw = dict(packet)
    kind = str(raw.get("kind") or "unknown").strip()[:80]
    source = str(raw.get("source") or kind or "unknown").strip()[:120]
    source_ref = str(raw.get("source_ref") or raw.get("ref") or "").strip()[:500]
    title = str(raw.get("title") or raw.get("name") or "").strip()[:300]
    excerpt = str(
        raw.get("excerpt") or raw.get("quote") or raw.get("content") or ""
    ).strip()[:2000]
    metadata = (
        dict(raw.get("metadata") or {})
        if isinstance(raw.get("metadata"), Mapping)
        else {}
    )
    document_id = str(raw.get("source_document_id") or "").strip()
    provenance = resolve_provenance_status(
        source_document_id=document_id,
        provenance_status=str(raw.get("provenance_status") or ""),
        metadata=metadata,
    )
    metadata["provenance_status"] = provenance
    if provenance == "cache_only":
        metadata["cache_only"] = True
    item = {
        "id": str(raw.get("id") or raw.get("evidence_id") or "").strip(),
        "kind": kind,
        "source": source,
        "source_ref": source_ref,
        "title": title,
        "url": str(raw.get("url") or "").strip()[:1000],
        "observed_at": str(
            raw.get("observed_at") or raw.get("timestamp") or ""
        ).strip()[:80],
        "as_of": str(raw.get("as_of") or "").strip()[:80],
        "published_at": str(raw.get("published_at") or "").strip()[:80],
        "retrieved_at": str(raw.get("retrieved_at") or "").strip()[:80],
        "valid_from": str(raw.get("valid_from") or "").strip()[:80],
        "valid_to": str(raw.get("valid_to") or "").strip()[:80],
        "excerpt": excerpt,
        "quality_status": str(raw.get("quality_status") or "unknown").strip().lower(),
        "source_document_id": document_id,
        "provenance_status": provenance,
        "metadata": metadata,
    }
    if not item["id"]:
        item["id"] = evidence_id(
            item["kind"],
            source_ref=item["source_ref"],
            title=item["title"],
            text=item["excerpt"],
        )
    return item


def evidence_temporal_status(item: Mapping[str, Any]) -> str:
    """回傳來源時間一致性狀態；未知 metadata 不自行推測為一致。"""
    metadata = item.get("metadata")
    if isinstance(metadata, Mapping):
        status = str(metadata.get("temporal_status") or "").strip().lower()
        if status in {"aligned", "mixed", "unknown", "rejected"}:
            return status
    return "unknown"


def is_evidence_temporally_acceptable(
    item: Mapping[str, Any],
    policy: str = "warn_mixed",
) -> bool:
    """依查詢政策判斷是否可以把跨日證據放入回答上下文。

    ``warn_mixed`` 保留跨日資料但讓上層顯示警告；``same_date`` 則只接受
    adapter 已明確標記為同日的證據。沒有時間一致性 metadata 時，嚴格政策
    採保守拒絕，避免舊 adapter 被誤當成同時點資料。
    """
    normalized = str(policy or "warn_mixed").strip().lower()
    if normalized != "same_date":
        return True
    return evidence_temporal_status(item) == "aligned"


def _parse_observed_at(value: Any) -> dt.datetime | None:
    """解析常見 ISO/date 格式，無時區值一律以台北時間解讀。"""
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            parsed = None
            for fmt in (
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d",
                "%Y%m%d%H%M%S",
                "%Y%m%d",
                "%Y-%m-%d",
            ):
                try:
                    parsed = dt.datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TW_TZ)
    return parsed.astimezone(TW_TZ)


def is_evidence_fresh(
    item: Mapping[str, Any],
    freshness: str,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """判斷證據是否符合 QueryPlan 的時間要求。

    ``as_available``／``unbounded`` 不做時間淘汰。期間型資料使用
    ``retrieved_at`` 判斷「最近是否重新取得」，而不是把財務期間的
    ``as_of`` 誤當成抓取時間。估值／股利嚴格窗口只接受 ``retrieved_at``；
    缺擷取時間視為過期。其他期間型保留 ``observed_at`` fallback 相容舊資料。
    """
    policy = str(freshness or "as_available").strip().lower()
    if policy in {"as_available", "unbounded"}:
        return True
    window = FRESHNESS_WINDOWS.get(policy)
    if window is None:
        return False
    kind = str(item.get("kind") or "").strip().lower()
    if kind in _RETRIEVAL_ONLY_PERIOD_KINDS:
        # 估值／股利的 observed_at 是資料期間，不能取代缺失的擷取時間。
        observed = _parse_observed_at(item.get("retrieved_at"))
    elif kind in PERIOD_BASED_KINDS:
        observed = _parse_observed_at(
            item.get("retrieved_at") or item.get("observed_at")
        )
    elif kind == "news":
        observed = _parse_observed_at(
            item.get("published_at") or item.get("observed_at") or item.get("retrieved_at")
        )
    else:
        observed = _parse_observed_at(
            item.get("observed_at")
            or item.get("as_of")
            or item.get("published_at")
            or item.get("retrieved_at")
        )
    if observed is None:
        return False
    reference = now or now_tw()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=TW_TZ)
    else:
        reference = reference.astimezone(TW_TZ)
    age = reference - observed
    return dt.timedelta(0) <= age <= window


@dataclass
class EvidencePack:
    """一次 QueryPlan 實際取得的證據集合。"""

    query: str
    plan_id: str
    trace_id: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    missing_resources: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    resource_outcomes: List[Dict[str, Any]] = field(default_factory=list)
    # ok / no_evidence / resource_failed / timeout / live_disabled / partial
    collection_status: str = "ok"

    def __post_init__(self) -> None:
        normalized: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in self.items:
            if not isinstance(item, Mapping):
                continue
            clean = normalize_evidence_packet(item)
            if clean["id"] in seen:
                continue
            seen.add(clean["id"])
            normalized.append(clean)
        self.items = normalized
        self.resource_outcomes = [
            dict(row) for row in self.resource_outcomes if isinstance(row, Mapping)
        ]
        status = str(self.collection_status or "ok").strip().lower()
        if status not in {
            "ok", "no_evidence", "resource_failed", "timeout",
            "live_disabled", "partial",
        }:
            status = "ok"
        self.collection_status = status

    @property
    def evidence_ids(self) -> List[str]:
        return [str(item["id"]) for item in self.items]

    @property
    def usable_items(self) -> List[Dict[str, Any]]:
        return [
            item for item in self.items
            if item.get("quality_status") in {"ok", "partial"}
            and str(item.get("excerpt") or "").strip()
        ]

    @property
    def market_usable_items(self) -> List[Dict[str, Any]]:
        """可支持市場／官方事實主張的證據；排除對話上下文。"""
        return [
            item for item in self.usable_items
            if not is_conversation_evidence(item)
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "plan_id": self.plan_id,
            "trace_id": self.trace_id,
            "items": list(self.items),
            "missing_resources": list(self.missing_resources),
            "warnings": list(self.warnings),
            "resource_outcomes": list(self.resource_outcomes),
            "collection_status": self.collection_status,
        }

    def to_prompt_text(self, *, max_chars: int = 16_000) -> str:
        lines: List[str] = []
        for item in self.usable_items:
            time_text = (
                f"as_of={item['as_of']} " if item.get("as_of") else ""
            ) + (
                f"published_at={item['published_at']} "
                if item.get("published_at") else ""
            ) + (
                f"retrieved_at={item['retrieved_at']}"
                if item.get("retrieved_at") else ""
            )
            provenance = str(
                item.get("provenance_status")
                or (item.get("metadata") or {}).get("provenance_status")
                or "unresolved"
            )
            role = "對話上下文（非市場事實）" if is_conversation_evidence(item) else "可引用來源"
            provenance_note = {
                "exact": "可回放 raw document",
                "composite_exact": "多個可回放 raw document，完整來源見 evidence links",
                "cache_only": "本地快取，非官方 raw document",
                "unresolved": "尚無可證明 raw document",
                "legacy_unlinked": "legacy 未連結來源",
            }.get(provenance, provenance)
            lines.append(
                f"[{item['id']}] {item['kind']} / {item['source']} / "
                f"{time_text or item['observed_at'] or 'unknown time'}\n"
                f"角色：{role}；provenance={provenance}（{provenance_note}）\n"
                f"標題：{item['title'] or '(無)'}\n"
                f"原文片段：{item['excerpt']}"
            )
        return "\n\n".join(lines)[:max_chars]


def build_evidence_pack(
    query: str,
    *,
    plan_id: str,
    trace_id: str,
    items: Iterable[Mapping[str, Any]],
    missing_resources: Iterable[str] = (),
    warnings: Iterable[str] = (),
    resource_outcomes: Iterable[Mapping[str, Any]] = (),
    collection_status: str = "ok",
) -> EvidencePack:
    return EvidencePack(
        query=str(query),
        plan_id=str(plan_id),
        trace_id=str(trace_id),
        items=[dict(item) for item in items],
        missing_resources=[str(v) for v in missing_resources if str(v).strip()],
        warnings=[str(v) for v in warnings if str(v).strip()],
        resource_outcomes=[dict(row) for row in resource_outcomes],
        collection_status=str(collection_status or "ok"),
    )


def persist_evidence_items(db: Any, items: Iterable[Mapping[str, Any]]) -> int:
    """把本次取得的 evidence packet 寫入中央 registry。

    這個步驟只登錄來源與證據，不代表主張已被 LLM 證明。EvidencePack
    在送入回答模型前就應先完成登錄，讓同一筆回答的 ``evidence_ids`` 能
    反向找到可重播的來源索引；原始內容仍由 source registry 保存。
    """
    # 延遲 import，避免 evidence.py 與 stock_db 的 dataclass 互相初始化。
    from bot.stock_db import EvidenceItemRow, EvidenceLinkRow

    count = 0
    for packet in items:
        if not isinstance(packet, Mapping):
            continue
        item = normalize_evidence_packet(packet)
        excerpt = str(item.get("excerpt") or "")
        hash_basis = "|".join((
            str(item.get("title") or ""),
            excerpt,
            str(item.get("source_ref") or ""),
            str(item.get("url") or ""),
        ))
        metadata = dict(item.get("metadata") or {})
        provenance = resolve_provenance_status(
            source_document_id=str(item.get("source_document_id") or ""),
            provenance_status=str(item.get("provenance_status") or ""),
            metadata=metadata,
        )
        metadata["provenance_status"] = provenance
        db.upsert_evidence(EvidenceItemRow(
            evidence_id=str(item["id"]),
            kind=str(item.get("kind") or "unknown"),
            source=str(item.get("source") or ""),
            source_ref=str(item.get("source_ref") or ""),
            title=str(item.get("title") or ""),
            url=str(item.get("url") or ""),
            observed_at=str(item.get("observed_at") or ""),
            as_of=str(item.get("as_of") or ""),
            published_at=str(item.get("published_at") or ""),
            retrieved_at=str(item.get("retrieved_at") or ""),
            valid_from=str(item.get("valid_from") or ""),
            valid_to=str(item.get("valid_to") or ""),
            excerpt=excerpt,
            quality_status=str(item.get("quality_status") or "unknown"),
            content_hash=hashlib.sha256(hash_basis.encode("utf-8")).hexdigest(),
            metadata_json=json.dumps(
                metadata,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
            source_document_id=str(item.get("source_document_id") or ""),
        ))
        source_document_ids = []
        primary_source_document_id = str(item.get("source_document_id") or "")
        if primary_source_document_id:
            source_document_ids.append(primary_source_document_id)
        raw_source_document_ids = metadata.get("source_document_ids")
        if isinstance(raw_source_document_ids, list):
            source_document_ids.extend(
                str(value).strip()
                for value in raw_source_document_ids
                if str(value).strip()
            )
        # Links are created only from explicit source identities in the
        # packet; no URL/title matching is performed here.  Composite
        # evidence therefore keeps every endpoint in the relational layer.
        for source_document_id in dict.fromkeys(source_document_ids):
            try:
                # 一個穩定 evidence ID 可能在不同抓取版本中重用；來源
                # 文件連結必須以 source-document identity 分開保存，不能
                # 讓後一次 upsert 覆蓋前一個版本的 provenance。
                source_digest = hashlib.sha256(
                    source_document_id.encode("utf-8")
                ).hexdigest()[:16]
                db.upsert_evidence_link(EvidenceLinkRow(
                    link_id=f"evidence-source:{item['id']}:{source_digest}",
                    evidence_id=str(item["id"]),
                    source_document_id=source_document_id,
                    subject_type="source_document",
                    subject_id=source_document_id,
                    quote_or_summary=excerpt,
                    as_of=str(item.get("as_of") or item.get("observed_at") or ""),
                ))
            except Exception:
                # The evidence item itself remains available for this request;
                # audit will expose a failed link rather than guessing another source.
                pass
        count += 1
    return count


__all__ = [
    "EvidencePack",
    "FRESHNESS_WINDOWS",
    "PERIOD_BASED_KINDS",
    "PROVENANCE_STATUSES",
    "CONVERSATION_EVIDENCE_KINDS",
    "build_evidence_pack",
    "evidence_id",
    "evidence_temporal_status",
    "is_conversation_evidence",
    "is_evidence_fresh",
    "is_evidence_temporally_acceptable",
    "make_evidence",
    "normalize_evidence_packet",
    "persist_evidence_items",
    "resolve_provenance_status",
]

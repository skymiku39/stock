"""可追溯的研究證據項目。

LLM 可以使用摘要與原文，但下游保存時應保留穩定的 evidence id，避免只有
「新聞」「網路搜尋」這種無法回查的來源標籤。
"""

from __future__ import annotations

import datetime as dt
import hashlib
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
PERIOD_BASED_KINDS = {"fundamental", "technical", "chips", "etf", "macro"}


def evidence_id(kind: str, *, source_ref: str = "", title: str = "", text: str = "") -> str:
    seed = "|".join((kind, source_ref, title, text[:500]))
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


def make_evidence(
    kind: str,
    *,
    source: str,
    source_ref: str = "",
    title: str = "",
    url: str = "",
    observed_at: str = "",
    excerpt: str = "",
    quality_status: str = "ok",
) -> Dict[str, Any]:
    title = str(title or "").strip()
    excerpt = str(excerpt or "").strip()
    return {
        "id": evidence_id(kind, source_ref=source_ref, title=title, text=excerpt),
        "kind": str(kind or "unknown"),
        "source": str(source or "unknown"),
        "source_ref": str(source_ref or ""),
        "title": title[:300],
        "url": str(url or ""),
        "observed_at": str(observed_at or ""),
        "excerpt": excerpt[:2000],
        "quality_status": str(quality_status or "unknown"),
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
        "excerpt": excerpt,
        "quality_status": str(raw.get("quality_status") or "unknown").strip().lower(),
    }
    if not item["id"]:
        item["id"] = evidence_id(
            item["kind"],
            source_ref=item["source_ref"],
            title=item["title"],
            text=item["excerpt"],
        )
    return item


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

    ``as_available``／``unbounded`` 不做時間淘汰。對 fundamental 保留期間型
    資料，因為月營收與季報的日期是 as-of period；其「最新可得」判斷應由
    fetcher 的來源日期與資料刷新流程處理。其餘嚴格 freshness 若缺少或無法
    解析觀測時間，採保守策略視為過期。
    """
    policy = str(freshness or "as_available").strip().lower()
    if policy in {"as_available", "unbounded"}:
        return True
    window = FRESHNESS_WINDOWS.get(policy)
    if window is None:
        return False
    if str(item.get("kind") or "").strip().lower() in PERIOD_BASED_KINDS:
        return True
    observed = _parse_observed_at(item.get("observed_at"))
    if observed is None:
        return False
    reference = now or now_tw()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=TW_TZ)
    else:
        reference = reference.astimezone(TW_TZ)
    age = reference - observed
    return age <= window


@dataclass
class EvidencePack:
    """一次 QueryPlan 實際取得的證據集合。"""

    query: str
    plan_id: str
    trace_id: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    missing_resources: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "plan_id": self.plan_id,
            "trace_id": self.trace_id,
            "items": list(self.items),
            "missing_resources": list(self.missing_resources),
            "warnings": list(self.warnings),
        }

    def to_prompt_text(self, *, max_chars: int = 16_000) -> str:
        lines: List[str] = []
        for item in self.usable_items:
            lines.append(
                f"[{item['id']}] {item['kind']} / {item['source']} / "
                f"{item['observed_at'] or 'unknown time'}\n"
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
) -> EvidencePack:
    return EvidencePack(
        query=str(query),
        plan_id=str(plan_id),
        trace_id=str(trace_id),
        items=[dict(item) for item in items],
        missing_resources=[str(v) for v in missing_resources if str(v).strip()],
        warnings=[str(v) for v in warnings if str(v).strip()],
    )


__all__ = [
    "EvidencePack",
    "FRESHNESS_WINDOWS",
    "PERIOD_BASED_KINDS",
    "build_evidence_pack",
    "evidence_id",
    "is_evidence_fresh",
    "make_evidence",
    "normalize_evidence_packet",
]

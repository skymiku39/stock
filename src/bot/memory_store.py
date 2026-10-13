"""本地優先的短期／長期記憶儲存層。

這個模組不把所有資料都叫做「記憶」：行情、營收與財報仍由 canonical tables
管理；記憶只保存 LLM 工作上下文、研究摘要、偏好、決策與可回放的結論。

設計原則：

* ``content`` 保存完整原文，``summary`` 只用於檢索與上下文壓縮。
* 短期記憶到期或超過容量時只標成 ``archived``，不硬刪除。
* 長期記憶必須帶來源、時間、品質、信心與 evidence reference。
* 所有建立、升級、封存與刪除都是 ``memory_events``，可追蹤誰在何時改了什麼。
* FTS5 只是索引；索引損壞可以由 ``StockDB._init_schema`` 重建，資料不依賴索引存在。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from bot.data_quality import normalize_quality_status
from bot.evidence import resolve_provenance_status
from bot.source_registry import is_source_document_replayable
from bot.stock_db import (
    EvidenceItemRow,
    LlmAnalysisRow,
    LlmDailyReportRow,
    MemoryEventRow,
    MemoryItemRow,
    MemoryProposalRow,
    StockDB,
    get_db,
)
from bot.utils import now_tw

# 長期記憶只用可回放的 exact／composite_exact；cache_only／unresolved／legacy 一律拒絕，不臆造。
_LONG_MEMORY_PROVENANCE_ALLOWED = frozenset({"exact", "composite_exact"})
_LONG_MEMORY_PROVENANCE_REJECTED = frozenset({
    "cache_only",
    "unresolved",
    "legacy_unlinked",
    "legacy",
})
# 市場／研究結論型長期記憶必須帶可回放 evidence；preference／context 等非市場類型除外。
LONG_MEMORY_EVIDENCE_REQUIRED_TYPES = frozenset({
    "research_summary",
    "daily_report",
    "decision",
})


@dataclass
class MemoryPolicy:
    """邏輯容量上限；封存不等於刪除，完整內容仍可回放。"""

    short_ttl_days: int = 14
    max_active_short_items: int = 500
    max_active_short_chars: int = 2_000_000
    max_active_long_items: int = 10_000
    max_active_long_chars: int = 8_000_000
    capacity_warning_ratio: float = 0.80


@dataclass
class MemoryContext:
    query: str
    short: List[MemoryItemRow] = field(default_factory=list)
    long: List[MemoryItemRow] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "short": [asdict(item) for item in self.short],
            "long": [asdict(item) for item in self.long],
            "truncated": self.truncated,
        }


@dataclass
class PromoteOutcome:
    """短期升級長期的統一結果；CLI 與 Python API 共用。"""

    ok: bool
    memory: Optional[MemoryItemRow] = None
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "memory": asdict(self.memory) if self.memory is not None else None,
            "reasons": list(self.reasons),
        }


def _json(value: Any, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value or default
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _new_id(prefix: str = "memory") -> str:
    return f"{prefix}:{now_tw().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:12]}"


def _evidence_ids(evidence: Any) -> List[str]:
    if not evidence:
        return []
    values = evidence if isinstance(evidence, (list, tuple, set)) else [evidence]
    out: List[str] = []
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("id") or value.get("evidence_id") or ""
        text = _text(value)
        if text and text not in out:
            out.append(text)
    return out


def _evidence_records(evidence: Any) -> List[tuple[str, Mapping[str, Any]]]:
    """把外部 evidence packet 正規化成中央 registry 可保存的資料。"""
    if not evidence:
        return []
    values = evidence if isinstance(evidence, (list, tuple, set)) else [evidence]
    out: List[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        raw = dict(value)
        evidence_id = _text(raw.get("id") or raw.get("evidence_id"))
        if not evidence_id:
            fingerprint = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
            evidence_id = "evidence:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        out.append((evidence_id, raw))
    return out


def _decode_json_object(value: str) -> Dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _evidence_metadata_for_registry(packet: Mapping[str, Any]) -> Dict[str, Any]:
    """把 packet 的 provenance／document ID 寫進 metadata，供長期閘門回放。"""
    metadata = (
        dict(packet.get("metadata") or {})
        if isinstance(packet.get("metadata"), Mapping)
        else {}
    )
    document_id = _text(packet.get("source_document_id"))
    linked = packet.get("source_document_ids")
    if isinstance(linked, list):
        metadata.setdefault(
            "source_document_ids",
            [str(value).strip() for value in linked if str(value).strip()],
        )
    provenance = resolve_provenance_status(
        source_document_id=document_id,
        provenance_status=_text(packet.get("provenance_status")),
        metadata=metadata,
    )
    metadata["provenance_status"] = provenance
    if provenance == "cache_only":
        metadata["cache_only"] = True
    return metadata


def _evidence_row_provenance(row: EvidenceItemRow) -> str:
    metadata = _decode_json_object(row.metadata_json)
    return resolve_provenance_status(
        source_document_id=_text(row.source_document_id),
        provenance_status=_text(metadata.get("provenance_status")),
        metadata=metadata,
    )


def _evidence_row_document_ids(row: EvidenceItemRow) -> List[str]:
    """收集可回放檢查用的 document ID；不臆造缺漏的 ID。"""
    document_ids: List[str] = []
    primary = _text(row.source_document_id)
    if primary:
        document_ids.append(primary)
    metadata = _decode_json_object(row.metadata_json)
    linked = metadata.get("source_document_ids")
    if isinstance(linked, list):
        document_ids.extend(
            str(value).strip() for value in linked if str(value).strip()
        )
    return list(dict.fromkeys(document_ids))


def requires_long_memory_evidence(memory_type: str) -> bool:
    """研究／報告／決策型長期記憶必須通過 exact evidence 閘門。"""
    return (_text(memory_type) or "context") in LONG_MEMORY_EVIDENCE_REQUIRED_TYPES


def _parse_time(value: str) -> Optional[dt.datetime]:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now_tw().tzinfo)
    return parsed


class MemoryStore:
    """以 SQLite 為唯一真實來源的記憶管理器。"""

    def __init__(
        self,
        db: Optional[StockDB] = None,
        *,
        policy: Optional[MemoryPolicy] = None,
        actor: str = "system",
    ) -> None:
        self.db = db or get_db()
        self.policy = policy or MemoryPolicy()
        self.actor = actor
        db_path = Path(self.db.path)
        self.root = (
            db_path.parent.parent
            if db_path.parent.name.lower() == "data"
            else db_path.parent
        )

    def _should_gate_long_evidence(self, memory_type: str, evidence: Any) -> bool:
        """只對研究／報告／決策型長期記憶啟用 evidence 閘門。"""
        return requires_long_memory_evidence(memory_type)

    def _upsert_evidence_packet(
        self,
        evidence_id: str,
        evidence_packet: Mapping[str, Any],
        *,
        quality_status: str = "unknown",
    ) -> None:
        excerpt = _text(
            evidence_packet.get("excerpt")
            or evidence_packet.get("quote")
            or evidence_packet.get("content")
        )
        title = _text(evidence_packet.get("title") or evidence_packet.get("name"))
        source = _text(evidence_packet.get("source") or evidence_packet.get("kind"))
        source_ref = _text(
            evidence_packet.get("source_ref")
            or evidence_packet.get("ref")
            or evidence_packet.get("message_id")
        )
        hash_basis = "|".join((title, excerpt, source_ref, _text(evidence_packet.get("url"))))
        self.db.upsert_evidence(EvidenceItemRow(
            evidence_id=evidence_id,
            kind=_text(evidence_packet.get("kind")) or "unknown",
            source=source,
            source_ref=source_ref,
            title=title,
            url=_text(evidence_packet.get("url")),
            observed_at=_text(
                evidence_packet.get("observed_at") or evidence_packet.get("timestamp")
            ),
            as_of=_text(evidence_packet.get("as_of")),
            published_at=_text(evidence_packet.get("published_at")),
            retrieved_at=_text(evidence_packet.get("retrieved_at")),
            valid_from=_text(evidence_packet.get("valid_from")),
            valid_to=_text(evidence_packet.get("valid_to")),
            excerpt=excerpt,
            quality_status=_text(evidence_packet.get("quality_status")) or quality_status,
            content_hash=hashlib.sha256(hash_basis.encode("utf-8")).hexdigest(),
            metadata_json=_json(
                _evidence_metadata_for_registry(evidence_packet), "{}"
            ),
            source_document_id=_text(evidence_packet.get("source_document_id")),
        ))

    def _event(
        self,
        memory_id: str,
        event_type: str,
        *,
        trace_id: str = "",
        payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.db.append_memory_event(MemoryEventRow(
            event_id=_new_id("memory-event"),
            memory_id=memory_id,
            event_type=event_type,
            actor=self.actor,
            trace_id=trace_id,
            payload_json=_json(payload or {}, "{}"),
        ))

    def put(
        self,
        content: Any,
        *,
        memory_layer: str,
        memory_type: str = "context",
        scope_type: str = "global",
        scope_id: str = "",
        subject: str = "",
        summary: str = "",
        keywords: Optional[Sequence[str] | str] = None,
        importance: float = 0.5,
        confidence: float = 0.0,
        quality_status: str = "unknown",
        source: str = "",
        source_ref: str = "",
        source_record_id: str = "",
        evidence: Any = None,
        metadata: Any = None,
        observed_at: str = "",
        expires_at: str = "",
        parent_id: str = "",
        memory_id: str = "",
        trace_id: str = "",
    ) -> MemoryItemRow:
        """建立或更新一筆記憶；完整 content 不會因 summary 而被截斷。"""
        if memory_layer not in {"short", "long"}:
            raise ValueError("memory_layer 必須是 short 或 long")
        full_content = content if isinstance(content, str) else _json(content, "")
        if not full_content.strip():
            raise ValueError("記憶 content 不可為空")
        now_iso = now_tw().isoformat(timespec="seconds")
        item_id = memory_id or _new_id("memory")
        keyword_text = keywords if isinstance(keywords, str) else ",".join(
            _text(value) for value in (keywords or []) if _text(value)
        )
        if not summary:
            summary = full_content[:1200]
        if memory_layer == "short" and not expires_at and self.policy.short_ttl_days > 0:
            expires_at = (
                now_tw() + dt.timedelta(days=self.policy.short_ttl_days)
            ).isoformat(timespec="seconds")
        evidence_records = _evidence_records(evidence)
        evidence_list = _evidence_ids(evidence)
        for evidence_id, _ in evidence_records:
            if evidence_id not in evidence_list:
                evidence_list.append(evidence_id)
        # 先登錄 packet，再跑共用閘門；避免 put_long 以未登錄 ID 繞過 exact／replayable 檢查。
        for evidence_id, evidence_packet in evidence_records:
            self._upsert_evidence_packet(
                evidence_id, evidence_packet, quality_status=quality_status,
            )
        resolved_type = _text(memory_type) or "context"
        if memory_layer == "long" and self._should_gate_long_evidence(
            resolved_type, evidence_list
        ):
            gate_reasons = self._mirror_long_memory_reasons(evidence_list)
            if gate_reasons:
                raise ValueError("; ".join(gate_reasons))
        # Idempotent deduplication for automatically generated memories.  An
        # explicit memory_id always wins because manual revision needs a stable
        # address.  Exact content/scope matching avoids semantic guessing.
        if not memory_id:
            duplicate = self.db.conn.execute(
                "SELECT memory_id FROM memory_items WHERE memory_layer=? "
                "AND memory_type=? AND scope_type=? AND scope_id=? AND subject=? "
                "AND content=? AND memory_status='active' LIMIT 1",
                (memory_layer, resolved_type,
                 _text(scope_type) or "global", _text(scope_id), _text(subject),
                 full_content),
            ).fetchone()
            if duplicate:
                item_id = str(duplicate[0])
        item = MemoryItemRow(
            memory_id=item_id,
            memory_layer=memory_layer,
            memory_type=resolved_type,
            scope_type=_text(scope_type) or "global",
            scope_id=_text(scope_id),
            subject=_text(subject),
            content=full_content,
            summary=_text(summary),
            keywords=_text(keyword_text),
            importance=max(0.0, min(1.0, float(importance))),
            confidence=max(0.0, min(1.0, float(confidence))),
            quality_status=normalize_quality_status(quality_status),
            memory_status="active",
            created_at=now_iso,
            observed_at=_text(observed_at) or now_iso,
            expires_at=_text(expires_at),
            source=_text(source),
            source_ref=_text(source_ref),
            source_record_id=_text(source_record_id),
            evidence_json=_json(evidence_list, "[]"),
            metadata_json=_json(metadata or {}, "{}"),
            parent_id=_text(parent_id),
            revision=1,
            updated_at=now_iso,
        )
        existing = self.db.get_memory_item(item_id)
        if existing:
            item.created_at = existing.created_at
            item.access_count = existing.access_count
            item.last_accessed_at = existing.last_accessed_at
            item.revision = existing.revision + 1
        self.db.upsert_memory_item(item)
        for evidence_id, evidence_packet in evidence_records:
            self._upsert_evidence_packet(
                evidence_id, evidence_packet, quality_status=quality_status,
            )
            self.db.link_memory_evidence(item_id, evidence_id)
        for evidence_id in evidence_list:
            if not any(evidence_id == registered_id for registered_id, _ in evidence_records):
                if self.db.get_evidence(evidence_id) is not None:
                    self.db.link_memory_evidence(item_id, evidence_id)
        self._event(
            item_id,
            "updated" if existing else "created",
            trace_id=trace_id,
            payload={"memory_layer": memory_layer, "memory_type": memory_type},
        )
        self.enforce_limits()
        return self.db.get_memory_item(item_id) or item

    def put_short(self, content: Any, **kwargs: Any) -> MemoryItemRow:
        return self.put(content, memory_layer="short", **kwargs)

    def put_long(self, content: Any, **kwargs: Any) -> MemoryItemRow:
        return self.put(content, memory_layer="long", **kwargs)

    def record_llm_call(self, record: Any, *, source_record_id: str = "") -> MemoryItemRow:
        """保存每次 LLM 呼叫的完整 prompt/output 到短期記憶。

        長期研究結論由 validated output 另行升級；失敗呼叫仍保留，但預設不會
        被搜尋結果當成可靠知識。
        """
        metadata = getattr(record, "metadata", {}) or {}
        ticker = _text(metadata.get("ticker") or metadata.get("symbol"))
        success = bool(getattr(record, "success", False))
        content = {
            "ts": getattr(record, "ts", ""),
            "prompt_id": getattr(record, "prompt_id", ""),
            "prompt_version": getattr(record, "prompt_version", ""),
            "model": getattr(record, "model", ""),
            "input": getattr(record, "input", ""),
            "output": getattr(record, "output", ""),
            "metadata": metadata,
            "success": success,
            "error": getattr(record, "error", ""),
        }
        source_id = source_record_id or (
            f"{getattr(record, 'ts', '')}:{getattr(record, 'prompt_id', '')}:{ticker or 'global'}"
        )
        return self.put_short(
            content,
            memory_type="llm_call",
            scope_type="ticker" if ticker else "global",
            scope_id=ticker,
            subject=ticker,
            summary=(getattr(record, "output", "") or getattr(record, "error", "") or "")[:1200],
            keywords=[getattr(record, "prompt_id", ""), ticker],
            importance=0.2 if success else 0.05,
            confidence=0.6 if success else 0.0,
            quality_status="ok" if success else "failed",
            source="llm_call",
            source_ref=source_id,
            source_record_id=source_id,
            metadata=metadata,
            observed_at=getattr(record, "ts", ""),
            memory_id=f"llm-call:{source_id}",
        )

    def remember_llm_analysis(self, row: LlmAnalysisRow) -> PromoteOutcome:
        """只有成功且通過品質與 provenance 閘門的分析才升級成長期研究記憶。"""
        if not row.success or not row.ticker:
            return PromoteOutcome(
                ok=False,
                reasons=["分析未成功或缺少 ticker，跳過長期記憶鏡像"],
            )
        if row.quality_status not in {"ok", "partial"}:
            return PromoteOutcome(
                ok=False,
                reasons=[f"quality_status 不可鏡像: {row.quality_status or 'unknown'}"],
            )
        evidence = []
        try:
            evidence = json.loads(row.evidence_json or "[]")
        except json.JSONDecodeError:
            evidence = []
        evidence_ids = _evidence_ids(evidence)
        gate_reasons = self._mirror_long_memory_reasons(evidence_ids)
        if gate_reasons:
            return PromoteOutcome(ok=False, reasons=gate_reasons)
        content = row.raw_output or json.dumps({
            "summary": row.summary,
            "drivers": row.drivers,
            "risks": row.risks,
            "sentiment": row.sentiment,
            "sentiment_score": row.sentiment_score,
            "confidence": row.confidence,
        }, ensure_ascii=False)
        memory = self.put_long(
            content,
            memory_id=f"analysis:{row.id}",
            memory_type="research_summary",
            scope_type="ticker",
            scope_id=row.ticker,
            subject=row.ticker,
            summary=row.summary,
            keywords=[row.ticker, row.sentiment, row.prompt_id],
            importance=0.75,
            confidence=row.confidence,
            quality_status=row.quality_status,
            source=row.source or "llm_analysis",
            source_ref=row.id,
            source_record_id=row.source_record_id or row.id,
            evidence=evidence_ids,
            metadata={"prompt_id": row.prompt_id, "model": row.model},
            observed_at=row.ts,
        )
        return PromoteOutcome(ok=True, memory=memory)

    def remember_daily_report(self, row: LlmDailyReportRow) -> PromoteOutcome:
        """把合格且通過 provenance 閘門的每日報告鏡像到長期記憶。

        ``llm_daily_reports`` 是報告的 canonical table，但不是每一筆報告都
        適合成為長期知識。失敗、未知或無效的 fallback 必須留在報告表供
        稽核，不能進入正常的長期記憶檢索。
        """
        quality_status = normalize_quality_status(row.quality_status)
        if quality_status not in {"ok", "partial"}:
            return PromoteOutcome(
                ok=False,
                reasons=[f"quality_status 不可鏡像: {row.quality_status or 'unknown'}"],
            )

        evidence = []
        try:
            evidence = json.loads(row.evidence_json or "[]")
        except json.JSONDecodeError:
            evidence = []
        evidence_ids = _evidence_ids(evidence)
        gate_reasons = self._mirror_long_memory_reasons(evidence_ids)
        if gate_reasons:
            return PromoteOutcome(ok=False, reasons=gate_reasons)
        content = row.payload_json or row.brief_md
        memory = self.put_long(
            content,
            memory_id=f"report:{row.report_type}:{row.report_date}:{row.mode}",
            memory_type="daily_report",
            scope_type="global",
            subject=row.report_type,
            summary=row.brief_md,
            keywords=[row.report_type, row.market_tone],
            importance=0.65,
            confidence=0.7 if quality_status == "ok" else 0.3,
            quality_status=quality_status,
            source="llm_daily_report",
            source_ref=f"{row.report_type}:{row.report_date}:{row.mode}",
            source_record_id=row.source_record_id,
            evidence=evidence_ids,
            metadata={"asof": row.asof, "prompt_id": row.prompt_id},
            observed_at=row.asof or row.generated_at,
        )
        return PromoteOutcome(ok=True, memory=memory)

    def search(
        self,
        query: str,
        *,
        layers: Sequence[str] = ("short", "long"),
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        include_low_quality: bool = False,
        limit: int = 20,
    ) -> List[MemoryItemRow]:
        rows = self.db.search_memory_rows(
            query,
            memory_layers=layers,
            scope_type=scope_type,
            scope_id=scope_id,
            include_low_quality=include_low_quality,
            limit=limit,
        )
        for row in rows:
            self.db.touch_memory_item(row.memory_id)
        return rows

    def build_context(
        self,
        query: str,
        *,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        max_chars: int = 24_000,
        per_layer_limit: int = 20,
    ) -> MemoryContext:
        """回傳分層上下文；短期與長期不混成同一個未標記字串。"""
        short = self.search(
            query, layers=("short",), scope_type=scope_type,
            scope_id=scope_id, limit=per_layer_limit,
        )
        long = self.search(
            query, layers=("long",), scope_type=scope_type,
            scope_id=scope_id, limit=per_layer_limit,
        )
        chosen_short: List[MemoryItemRow] = []
        chosen_long: List[MemoryItemRow] = []
        used = 0
        truncated = False
        for target, rows in ((chosen_short, short), (chosen_long, long)):
            for row in rows:
                cost = len(row.summary or row.content)
                if used + cost > max_chars:
                    truncated = True
                    continue
                target.append(row)
                used += cost
        return MemoryContext(
            query=query,
            short=chosen_short,
            long=chosen_long,
            truncated=truncated,
        )

    def propose(
        self,
        content: Any,
        *,
        proposal_type: str = "create",
        target_memory_id: str = "",
        memory_layer: str = "long",
        memory_type: str = "summary",
        scope_type: str = "global",
        scope_id: str = "",
        subject: str = "",
        summary: str = "",
        keywords: Optional[Sequence[str] | str] = None,
        importance: float = 0.5,
        confidence: float = 0.0,
        quality_status: str = "unknown",
        source: str = "manual",
        source_ref: str = "",
        evidence: Any = None,
        metadata: Any = None,
        proposal_id: str = "",
        proposer: str = "",
    ) -> MemoryProposalRow:
        """建立管理提案；只有 approve 才會變成可檢索記憶。"""
        full_content = content if isinstance(content, str) else _json(content, "")
        if not full_content.strip():
            raise ValueError("記憶提案 content 不可為空")
        keyword_text = keywords if isinstance(keywords, str) else ",".join(
            _text(value) for value in (keywords or []) if _text(value)
        )
        evidence_records = _evidence_records(evidence)
        evidence_ids = _evidence_ids(evidence)
        for evidence_id, _ in evidence_records:
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        proposal = MemoryProposalRow(
            proposal_id=proposal_id or _new_id("memory-proposal"),
            proposal_type=_text(proposal_type) or "create",
            target_memory_id=_text(target_memory_id),
            memory_layer="long" if memory_layer == "long" else "short",
            memory_type=_text(memory_type) or "summary",
            scope_type=_text(scope_type) or "global",
            scope_id=_text(scope_id),
            subject=_text(subject),
            content=full_content,
            summary=_text(summary) or full_content[:1200],
            keywords=_text(keyword_text),
            importance=max(0.0, min(1.0, float(importance))),
            confidence=max(0.0, min(1.0, float(confidence))),
            quality_status=normalize_quality_status(quality_status),
            source=_text(source) or "manual",
            source_ref=_text(source_ref),
            evidence_json=_json(evidence_ids, "[]"),
            metadata_json=_json(metadata or {}, "{}"),
            proposer=_text(proposer) or self.actor,
        )
        self.db.create_memory_proposal(proposal)
        for evidence_id, evidence_packet in evidence_records:
            # 證據可先登錄，但提案核准前不會進入可檢索 memory_items。
            excerpt = _text(
                evidence_packet.get("excerpt")
                or evidence_packet.get("quote")
                or evidence_packet.get("content")
            )
            title = _text(evidence_packet.get("title") or evidence_packet.get("name"))
            source_name = _text(evidence_packet.get("source") or evidence_packet.get("kind"))
            source_ref_value = _text(
                evidence_packet.get("source_ref") or evidence_packet.get("ref")
            )
            hash_basis = "|".join((title, excerpt, source_ref_value, _text(evidence_packet.get("url"))))
            self.db.upsert_evidence(EvidenceItemRow(
                evidence_id=evidence_id,
                kind=_text(evidence_packet.get("kind")) or "unknown",
                source=source_name,
                source_ref=source_ref_value,
                title=title,
                url=_text(evidence_packet.get("url")),
                observed_at=_text(
                    evidence_packet.get("observed_at") or evidence_packet.get("timestamp")
                ),
                as_of=_text(evidence_packet.get("as_of")),
                published_at=_text(evidence_packet.get("published_at")),
                retrieved_at=_text(evidence_packet.get("retrieved_at")),
                valid_from=_text(evidence_packet.get("valid_from")),
                valid_to=_text(evidence_packet.get("valid_to")),
                excerpt=excerpt,
                quality_status=_text(evidence_packet.get("quality_status")) or proposal.quality_status,
                content_hash=hashlib.sha256(hash_basis.encode("utf-8")).hexdigest(),
                metadata_json=_json(
                    _evidence_metadata_for_registry(evidence_packet), "{}"
                ),
                source_document_id=_text(evidence_packet.get("source_document_id")),
            ))
        self._event(
            f"proposal:{proposal.proposal_id}",
            "proposal_created",
            payload={"proposal_type": proposal.proposal_type},
        )
        return self.db.get_memory_proposal(proposal.proposal_id) or proposal

    def approve_proposal(
        self,
        proposal_id: str,
        *,
        reviewer: str = "",
        review_note: str = "",
    ) -> Optional[MemoryItemRow]:
        """核准提案並落地；重複核准會被拒絕，避免重複寫入。"""
        proposal = self.db.get_memory_proposal(proposal_id)
        if proposal is None or proposal.status != "pending":
            return None
        reviewer_name = reviewer or self.actor
        if proposal.proposal_type == "delete":
            if not proposal.target_memory_id or not self.delete(proposal.target_memory_id):
                return None
            self.db.review_memory_proposal(
                proposal_id, "approved", reviewer=reviewer_name,
                review_note=review_note, target_memory_id=proposal.target_memory_id,
            )
            self._event(
                f"proposal:{proposal_id}", "proposal_approved",
                payload={"target_memory_id": proposal.target_memory_id, "action": "delete"},
            )
            return self.db.get_memory_item(proposal.target_memory_id)

        metadata = _decode_json_object(proposal.metadata_json)
        metadata["approved_from_proposal"] = proposal_id
        if proposal.proposal_type == "update":
            # update 必須指向既有 active 記憶；不能因為 target 拼錯而偷偷
            # 變成一筆新的 memory。未提供新 evidence 時保留舊 linkage。
            if not proposal.target_memory_id:
                return None
            target = self.db.get_memory_item(proposal.target_memory_id)
            if target is None or target.memory_status != "active":
                return None
            try:
                old_evidence = json.loads(target.evidence_json or "[]")
            except json.JSONDecodeError:
                old_evidence = []
            try:
                new_evidence = json.loads(proposal.evidence_json or "[]")
            except json.JSONDecodeError:
                new_evidence = []
            if not isinstance(old_evidence, list):
                old_evidence = []
            if not isinstance(new_evidence, list):
                new_evidence = []
            evidence = list(dict.fromkeys([
                str(value) for value in [*old_evidence, *new_evidence]
                if str(value).strip()
            ]))
            metadata.setdefault("updated_from_revision", target.revision)
            memory_id = proposal.target_memory_id
            gate_type = proposal.memory_type or target.memory_type
            if target.memory_layer == "long" and self._should_gate_long_evidence(
                gate_type, evidence
            ):
                if self.long_memory_evidence_gate(evidence):
                    return None
        elif proposal.proposal_type == "consolidate":
            # 子記憶 ID 必須由管理者明確提供；接受 target_memory_id 的逗號
            # 格式，也接受 metadata.children / metadata.memory_ids 陣列。
            raw_children = metadata.get("children") or metadata.get("memory_ids")
            if isinstance(raw_children, str):
                child_ids = [value.strip() for value in raw_children.split(",")]
            elif isinstance(raw_children, list):
                child_ids = [str(value).strip() for value in raw_children]
            else:
                child_ids = [
                    value.strip() for value in proposal.target_memory_id.split(",")
                ]
            child_ids = list(dict.fromkeys(value for value in child_ids if value))
            if len(child_ids) < 2:
                return None
            children = [self.db.get_memory_item(value) for value in child_ids]
            if any(item is None or item.memory_status != "active" for item in children):
                return None
            child_rows = [item for item in children if item is not None]
            evidence: List[str] = []
            for child in child_rows:
                try:
                    child_evidence = json.loads(child.evidence_json or "[]")
                except json.JSONDecodeError:
                    child_evidence = []
                if isinstance(child_evidence, list):
                    evidence.extend(str(value) for value in child_evidence if str(value).strip())
            try:
                proposed_evidence = json.loads(proposal.evidence_json or "[]")
            except json.JSONDecodeError:
                proposed_evidence = []
            if isinstance(proposed_evidence, list):
                evidence.extend(str(value) for value in proposed_evidence if str(value).strip())
            evidence = list(dict.fromkeys(evidence))
            if self.long_memory_evidence_gate(evidence):
                return None
            metadata["children"] = child_ids
            quality_status = proposal.quality_status
            if any(normalize_quality_status(item.quality_status) != "ok" for item in child_rows):
                quality_status = "partial"
            item = self.put_long(
                proposal.content,
                memory_id=f"proposal:{proposal_id}",
                memory_type=proposal.memory_type,
                scope_type=proposal.scope_type,
                scope_id=proposal.scope_id,
                subject=proposal.subject,
                summary=proposal.summary,
                keywords=proposal.keywords,
                importance=max([proposal.importance, *(child.importance for child in child_rows)]),
                confidence=min([proposal.confidence, *(child.confidence for child in child_rows)]),
                quality_status=quality_status,
                source=proposal.source,
                source_ref=proposal.source_ref or ",".join(child_ids),
                source_record_id=proposal.source_ref,
                evidence=evidence,
                metadata=metadata,
                parent_id=child_ids[0],
            )
            for child in child_rows:
                self.db.update_memory_status(child.memory_id, "archived")
                self._event(
                    child.memory_id,
                    "archived",
                    payload={"consolidated_into": item.memory_id, "proposal_id": proposal_id},
                )
            self.db.review_memory_proposal(
                proposal_id, "approved", reviewer=reviewer_name,
                review_note=review_note, target_memory_id=item.memory_id,
            )
            self._event(
                item.memory_id,
                "approved_from_proposal",
                payload={"proposal_id": proposal_id, "reviewer": reviewer_name, "action": "consolidate"},
            )
            return item
        else:
            evidence = json.loads(proposal.evidence_json or "[]")
            if not isinstance(evidence, list):
                evidence = []
            memory_id = proposal.target_memory_id or f"proposal:{proposal_id}"
            if proposal.memory_layer == "long" and self._should_gate_long_evidence(
                proposal.memory_type, evidence
            ):
                if self.long_memory_evidence_gate(evidence):
                    return None
        item = self.put(
            proposal.content,
            memory_layer=proposal.memory_layer,
            memory_type=proposal.memory_type,
            scope_type=proposal.scope_type,
            scope_id=proposal.scope_id,
            subject=proposal.subject,
            summary=proposal.summary,
            keywords=proposal.keywords,
            importance=proposal.importance,
            confidence=proposal.confidence,
            quality_status=proposal.quality_status,
            source=proposal.source,
            source_ref=proposal.source_ref,
            evidence=evidence,
            metadata=metadata,
            memory_id=memory_id,
        )
        self.db.review_memory_proposal(
            proposal_id, "approved", reviewer=reviewer_name,
            review_note=review_note, target_memory_id=item.memory_id,
        )
        self._event(
            item.memory_id, "approved_from_proposal",
            payload={"proposal_id": proposal_id, "reviewer": reviewer_name},
        )
        return item

    def reject_proposal(
        self,
        proposal_id: str,
        *,
        reviewer: str = "",
        review_note: str = "",
    ) -> bool:
        proposal = self.db.get_memory_proposal(proposal_id)
        if proposal is None or proposal.status != "pending":
            return False
        reviewer_name = reviewer or self.actor
        changed = self.db.review_memory_proposal(
            proposal_id, "rejected", reviewer=reviewer_name,
            review_note=review_note,
        )
        if changed:
            self._event(
                f"proposal:{proposal_id}", "proposal_rejected",
                payload={"reviewer": reviewer_name, "review_note": review_note},
            )
        return changed

    def long_memory_evidence_gate(
        self,
        evidence_ids: Sequence[Any],
    ) -> List[str]:
        """長期記憶共用 provenance 閘門；回傳拒絕原因（空列表＝通過）。

        空 evidence 一律拒絕，避免 research／report／decision 路徑 vacuously
        通過。只接受 ``exact``／``composite_exact``，且所有 source document ID
        必須可在本地 raw archive 回放。``cache_only``／``unresolved``／``legacy``
        一律拒絕，不依 URL／標題臆造 document ID。
        """
        ids = [
            str(evidence_id).strip()
            for evidence_id in evidence_ids
            if str(evidence_id or "").strip()
        ]
        if not ids:
            return ["缺少可升級的 evidence"]

        reasons: List[str] = []
        missing_evidence: List[str] = []
        rejected_provenance: List[str] = []
        missing_documents: List[str] = []
        invalid_documents: List[str] = []

        for eid in ids:
            row = self.db.get_evidence(eid)
            if row is None:
                missing_evidence.append(eid)
                continue
            provenance = _evidence_row_provenance(row)
            if provenance in _LONG_MEMORY_PROVENANCE_REJECTED or (
                provenance not in _LONG_MEMORY_PROVENANCE_ALLOWED
            ):
                rejected_provenance.append(f"{eid}={provenance or 'unresolved'}")
                continue
            document_ids = _evidence_row_document_ids(row)
            if not document_ids:
                missing_documents.append(eid)
                continue
            for document_id in document_ids:
                document = self.db.get_source_document(document_id)
                if not is_source_document_replayable(self.root, document):
                    invalid_documents.append(document_id)

        if missing_evidence:
            reasons.append("缺少 evidence: " + ", ".join(missing_evidence))
        if rejected_provenance:
            reasons.append(
                "provenance 不可升級為長期記憶（僅 exact／composite_exact）: "
                + ", ".join(rejected_provenance)
            )
        if missing_documents:
            reasons.append(
                "exact／composite_exact evidence 缺少可回放 source_document_id: "
                + ", ".join(missing_documents)
            )
        if invalid_documents:
            reasons.append(
                "source_document_id 不是可回放的 exact ID: "
                + ", ".join(dict.fromkeys(invalid_documents))
            )
        return reasons

    def _conflicted_claim_reasons(self, evidence_ids: Sequence[Any]) -> List[str]:
        """回傳與 evidence 連結且尚未解決的 conflicted claim 原因。"""
        conflicted: List[str] = []
        for evidence_id in evidence_ids:
            eid = str(evidence_id or "").strip()
            if not eid:
                continue
            try:
                rows = self.db.conn.execute(
                    "SELECT c.claim_id FROM claims c "
                    "JOIN claim_evidence ce ON ce.claim_id = c.claim_id "
                    "WHERE ce.evidence_id = ? AND c.status = 'conflicted' LIMIT 5",
                    (eid,),
                ).fetchall()
            except Exception:
                rows = []
            conflicted.extend(str(row[0]) for row in rows)
        if not conflicted:
            return []
        return [
            "存在未解決 claim conflict: "
            + ", ".join(dict.fromkeys(conflicted))
        ]

    def _mirror_long_memory_reasons(self, evidence_ids: Sequence[Any]) -> List[str]:
        """mirror／put_long 共用閘門：空 evidence 拒絕，其餘含 conflict 檢查。"""
        reasons: List[str] = []
        reasons.extend(self.long_memory_evidence_gate(evidence_ids))
        reasons.extend(self._conflicted_claim_reasons(evidence_ids))
        return reasons

    def evaluate_promote(self, memory_id: str) -> PromoteOutcome:
        """檢查一筆短期記憶是否可升級；不寫入。"""
        old = self.db.get_memory_item(memory_id)
        if old is None:
            return PromoteOutcome(ok=False, reasons=["找不到記憶"])
        if old.memory_status != "active":
            return PromoteOutcome(ok=False, reasons=[f"記憶狀態不可升級: {old.memory_status}"])
        if old.memory_layer == "long":
            return PromoteOutcome(ok=True, memory=old, reasons=["已是長期記憶"])

        reasons: List[str] = []
        quality = normalize_quality_status(old.quality_status)
        raw_quality = str(old.quality_status or "").strip().lower()
        if raw_quality in {"unresolved", "failed", "unknown"} or quality in {
            "failed", "unknown", "invalid", "missing", "stale",
        }:
            reasons.append(f"quality_status 不可升級: {old.quality_status or 'unknown'}")
        if quality not in {"ok", "partial"}:
            if f"quality_status 不可升級: {old.quality_status or 'unknown'}" not in reasons:
                reasons.append(f"quality_status 必須是 ok 或 partial，目前為 {quality}")
        if old.memory_type == "conversation_turn":
            reasons.append("conversation_turn 不可升級為長期市場／研究事實")

        try:
            evidence_ids = json.loads(old.evidence_json or "[]")
        except json.JSONDecodeError:
            evidence_ids = []
            reasons.append("evidence_json 無法解析")
        if not isinstance(evidence_ids, list):
            evidence_ids = []
            reasons.append("evidence_json 必須是陣列")
        reasons.extend(self.long_memory_evidence_gate(evidence_ids))
        reasons.extend(self._conflicted_claim_reasons(evidence_ids))

        if reasons:
            return PromoteOutcome(ok=False, reasons=reasons)
        return PromoteOutcome(ok=True, memory=old)

    def promote(
        self,
        memory_id: str,
        *,
        summary: str = "",
        trace_id: str = "",
    ) -> PromoteOutcome:
        """將短期記憶升級為長期；不合格時保留原文並回傳拒絕原因。"""
        check = self.evaluate_promote(memory_id)
        if not check.ok:
            return check
        old = check.memory
        if old is None:
            return PromoteOutcome(ok=False, reasons=["找不到記憶"])
        if old.memory_layer == "long":
            return PromoteOutcome(ok=True, memory=old, reasons=["已是長期記憶"])
        new = self.put_long(
            old.content,
            memory_type=old.memory_type,
            scope_type=old.scope_type,
            scope_id=old.scope_id,
            subject=old.subject,
            summary=summary or old.summary,
            keywords=old.keywords,
            importance=max(old.importance, 0.6),
            confidence=old.confidence,
            quality_status=old.quality_status,
            source=old.source,
            source_ref=old.source_ref,
            source_record_id=old.source_record_id,
            evidence=json.loads(old.evidence_json or "[]"),
            metadata={"promoted_from": old.memory_id},
            observed_at=old.observed_at,
            parent_id=old.memory_id,
            trace_id=trace_id,
        )
        self.db.update_memory_status(old.memory_id, "superseded")
        self._event(old.memory_id, "promoted", trace_id=trace_id,
                    payload={"new_memory_id": new.memory_id})
        return PromoteOutcome(ok=True, memory=new)

    def consolidate(
        self,
        memory_ids: Sequence[str],
        summary: str,
        *,
        content: str = "",
        memory_type: str = "summary",
        scope_type: str = "global",
        scope_id: str = "",
        source: str = "memory_consolidation",
        trace_id: str = "",
    ) -> Optional[MemoryItemRow]:
        """以摘要建立長期記憶，子項目封存但保留原文與事件。"""
        ids = [str(value) for value in memory_ids if str(value)]
        children = [self.db.get_memory_item(value) for value in ids]
        children = [item for item in children if item is not None]
        if not children or not summary.strip():
            return None
        evidence: List[str] = []
        for child in children:
            try:
                evidence.extend(json.loads(child.evidence_json or "[]"))
            except json.JSONDecodeError:
                pass
        evidence = list(dict.fromkeys(evidence))
        if self.long_memory_evidence_gate(evidence):
            return None
        item = self.put_long(
            content or summary,
            memory_type=memory_type,
            scope_type=scope_type,
            scope_id=scope_id,
            summary=summary,
            importance=max(child.importance for child in children),
            confidence=min(child.confidence for child in children),
            quality_status="partial" if any(child.quality_status != "ok" for child in children) else "ok",
            source=source,
            source_ref=",".join(ids),
            evidence=evidence,
            metadata={"children": ids},
            parent_id=ids[0],
            trace_id=trace_id,
        )
        for child in children:
            self.db.update_memory_status(child.memory_id, "archived")
            self._event(child.memory_id, "archived", trace_id=trace_id,
                        payload={"consolidated_into": item.memory_id})
        return item

    def archive_expired(self, *, now: str = "") -> int:
        current = _parse_time(now) or now_tw()
        changed = 0
        for item in self.db.list_memory_items(memory_layer="short", memory_status="active", limit=100_000):
            expires = _parse_time(item.expires_at)
            if expires and expires <= current:
                self.db.update_memory_status(item.memory_id, "archived")
                self._event(item.memory_id, "archived", payload={"reason": "expired"})
                changed += 1
        return changed

    def enforce_limits(self) -> Dict[str, int]:
        """只封存低重要度項目，保留原文供日後重建或人工回放。"""
        self.archive_expired()
        archived_short = 0
        short = self.db.list_memory_items(memory_layer="short", memory_status="active", limit=100_000)
        short = sorted(short, key=lambda row: (row.importance, row.last_accessed_at or row.created_at))
        total_chars = sum(len(row.content) for row in short)
        while (
            len(short) - archived_short > self.policy.max_active_short_items
            or total_chars > self.policy.max_active_short_chars
        ) and archived_short < len(short):
            row = short[archived_short]
            self.db.update_memory_status(row.memory_id, "archived")
            self._event(row.memory_id, "archived", payload={"reason": "capacity"})
            total_chars -= len(row.content)
            archived_short += 1

        archived_long = 0
        long = self.db.list_memory_items(memory_layer="long", memory_status="active", limit=100_000)
        long = sorted(long, key=lambda row: (row.importance, row.last_accessed_at or row.created_at))
        total_long_chars = sum(len(row.content) for row in long)
        while (
            len(long) - archived_long > self.policy.max_active_long_items
            or total_long_chars > self.policy.max_active_long_chars
        ) and archived_long < len(long):
            row = long[archived_long]
            self.db.update_memory_status(row.memory_id, "archived")
            self._event(row.memory_id, "archived", payload={"reason": "capacity", "layer": "long"})
            total_long_chars -= len(row.content)
            archived_long += 1
        return {"archived_short": archived_short, "archived_long": archived_long}

    def capacity_report(self) -> Dict[str, Any]:
        """回報容量與 80% 警戒，不把警戒誤當成刪除。"""
        self.archive_expired()
        values: Dict[str, Any] = {}
        for layer, max_items, max_chars in (
            ("short", self.policy.max_active_short_items, self.policy.max_active_short_chars),
            ("long", self.policy.max_active_long_items, self.policy.max_active_long_chars),
        ):
            rows = self.db.list_memory_items(memory_layer=layer, memory_status="active", limit=100_000)
            count = len(rows)
            chars = sum(len(row.content) for row in rows)
            item_ratio = count / max_items if max_items > 0 else 1.0
            char_ratio = chars / max_chars if max_chars > 0 else 1.0
            values[layer] = {
                "active_items": count,
                "max_items": max_items,
                "active_chars": chars,
                "max_chars": max_chars,
                "item_ratio": round(item_ratio, 4),
                "char_ratio": round(char_ratio, 4),
                "warning": max(item_ratio, char_ratio) >= self.policy.capacity_warning_ratio,
            }
        return values

    def delete(self, memory_id: str, *, trace_id: str = "") -> bool:
        """軟刪除；保留資料與事件，避免記憶管理失去稽核能力。"""
        item = self.db.get_memory_item(memory_id)
        if item is None:
            return False
        self.db.update_memory_status(memory_id, "deleted")
        self._event(memory_id, "deleted", trace_id=trace_id)
        return True

    def backfill_llm_logs(
        self,
        log_dir: Optional[Any] = None,
        *,
        limit: int = 100_000,
    ) -> int:
        """把既有 JSONL LLM audit log 補進短期記憶，重跑具 idempotency。"""
        from bot.llm_log import LlmCallLogger

        logger = LlmCallLogger(log_dir=log_dir) if log_dir is not None else LlmCallLogger()
        count = 0
        for record in logger.iter_records():
            if count >= limit:
                break
            self.record_llm_call(record)
            count += 1
        return count

    def backfill_database(self, *, limit: int = 100_000) -> Dict[str, Any]:
        """把既有 SQLite LLM 索引補進記憶層，供 migration 後一次執行。"""
        analyses = self.db.list_llm_analysis(limit=limit)
        reports = self.db.list_llm_daily_reports(limit=limit)
        long_analyses = 0
        skip_reasons: Counter[str] = Counter()
        for row in analyses:
            outcome = self.remember_llm_analysis(row)
            if outcome.ok and outcome.memory is not None:
                long_analyses += 1
                continue
            if not row.success:
                skip_reasons["llm_failed"] += 1
            elif not _text(row.ticker):
                skip_reasons["missing_ticker"] += 1
            elif normalize_quality_status(row.quality_status) not in {"ok", "partial"}:
                skip_reasons["low_quality"] += 1
            else:
                # 保留一個明確的 bucket，避免未來 remember_llm_analysis 新增
                # gate 後，backfill 只顯示「0」卻沒有可追查原因。
                skip_reasons["memory_gate"] += 1
        for row in reports:
            self.remember_daily_report(row)
        return {
            "llm_analysis_rows": len(analyses),
            "long_analyses": long_analyses,
            "long_analysis_skipped": sum(skip_reasons.values()),
            "skip_reasons": dict(sorted(skip_reasons.items())),
            "daily_reports": len(reports),
        }

    def stats(self) -> Dict[str, int]:
        rows = self.db.conn.execute(
            "SELECT memory_layer, memory_status, COUNT(*) AS n "
            "FROM memory_items GROUP BY memory_layer, memory_status"
        ).fetchall()
        out: Dict[str, int] = {}
        for row in rows:
            out[f"{row['memory_layer']}_{row['memory_status']}"] = int(row["n"])
        # Keep zero-valued buckets explicit.  CLI／Dashboard consumers must be
        # able to distinguish an empty long-term layer from a missing metric.
        for layer in ("short", "long"):
            for status in ("active", "archived", "superseded", "deleted"):
                out.setdefault(f"{layer}_{status}", 0)
        event_row = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM memory_events"
        ).fetchone()
        out["events"] = int(event_row["n"]) if event_row else 0
        evidence_row = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM evidence_items"
        ).fetchone()
        out["evidence_items"] = int(evidence_row["n"]) if evidence_row else 0
        proposal_rows = self.db.conn.execute(
            "SELECT status, COUNT(*) AS n FROM memory_proposals GROUP BY status"
        ).fetchall()
        for row in proposal_rows:
            out[f"proposals_{row['status']}"] = int(row["n"])
        return out


__all__ = [
    "LONG_MEMORY_EVIDENCE_REQUIRED_TYPES",
    "MemoryContext",
    "MemoryItemRow",
    "MemoryPolicy",
    "MemoryProposalRow",
    "MemoryStore",
    "PromoteOutcome",
    "requires_long_memory_evidence",
]

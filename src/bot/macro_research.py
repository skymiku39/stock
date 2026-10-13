"""跨市場 macro snapshot 的 research／evidence／claim 垂直切片。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from bot.evidence import make_evidence, persist_evidence_items
from bot.market_macro import macro_quality_status
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.stock_db import (
    ClaimEvidenceRow,
    ClaimRow,
    LlmRunEvidenceRow,
    LlmRunRow,
    StockDB,
)
from bot.utils import now_tw

PIPELINE = "market_macro_snapshot_slice"
INGEST_TASK_TYPE = "market_macro_snapshot_ingest"
INGEST_RUN_STATUS = "source_ingested"


@dataclass
class MacroSnapshotSlice:
    source_document_id: str
    research_document_id: str = ""
    evidence_id: str = ""
    run_id: str = ""
    claim_ids: List[str] = field(default_factory=list)
    chunk_ids: List[str] = field(default_factory=list)
    status: str = "ok"  # ok / reused / failed / empty
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def macro_evidence_excerpt(snapshot: Mapping[str, Any]) -> str:
    """與 !ask 共用穩定摘要，避免同一 raw snapshot 產生重複 evidence。"""
    keys = [
        "asof_date", "source_date_status", "usdtwd", "sox", "nasdaq",
        "spx", "vix", "taiex", "tx_basis", "notes",
    ]
    bits: List[str] = []
    for key in keys:
        value = snapshot.get(key)
        if value not in (None, "", [], {}):
            bits.append(f"{key}={value}")
    if not bits:
        bits.append(json.dumps(dict(snapshot), ensure_ascii=False)[:800])
    return "；".join(str(value) for value in bits)[:2000]


def _raw_archive_matches(root: Path, source: Any) -> tuple[bool, str]:
    path = resolve_source_document_path(root, source)
    if not path.is_file():
        return False, f"raw archive 不存在: {source.content_path}"
    try:
        content = path.read_bytes()
    except OSError as exc:
        return False, f"raw archive 無法讀取: {exc}"
    digest = hashlib.sha256(content).hexdigest()
    if source.content_hash and digest != source.content_hash:
        return False, "raw archive hash mismatch"
    return True, content.decode("utf-8", errors="replace")


def materialize_macro_snapshot_slice(
    *,
    root: Path,
    source_document_id: str,
    snapshot: Mapping[str, Any],
    db: Optional[StockDB] = None,
) -> MacroSnapshotSlice:
    """由已知 macro raw source 建立可回放研究資料與欄位 claims。"""
    project_root = Path(root)
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    try:
        source = database.get_source_document(source_document_id)
        if source is None:
            return MacroSnapshotSlice(
                source_document_id=source_document_id,
                status="failed",
                error=f"unknown source_document_id: {source_document_id}",
            )
        valid, raw_text = _raw_archive_matches(project_root, source)
        if not valid:
            return MacroSnapshotSlice(
                source_document_id=source_document_id,
                status="failed",
                error=raw_text,
            )
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return MacroSnapshotSlice(
                source_document_id=source_document_id,
                status="failed",
                error=f"macro raw JSON 無法解析: {exc}",
            )
        if not isinstance(raw, dict):
            return MacroSnapshotSlice(
                source_document_id=source_document_id,
                status="failed",
                error="macro raw JSON 根節點不是 object",
            )
        data = dict(snapshot)
        quality_input = dict(raw)
        quality_input.update(data)
        quality_status = macro_quality_status(quality_input)
        asof = str(data.get("asof_date") or raw.get("asof_date") or source.as_of)
        if not asof:
            return MacroSnapshotSlice(
                source_document_id=source_document_id,
                status="empty",
                error="macro snapshot 缺少 asof_date",
            )
        summary = {
            "asof_date": asof,
            "source_date_status": data.get("source_date_status") or raw.get("source_date_status") or "unknown",
            "indices_count": len(data.get("indices") or raw.get("indices") or {}),
            "stocks_count": len(data.get("stocks") or raw.get("stocks") or {}),
            "adr_premiums_count": len(data.get("adr_premiums") or raw.get("adr_premiums") or []),
            "usdtwd": data.get("usdtwd", raw.get("usdtwd")),
        }
        canonical_text = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2)
        research_id = "research-doc:" + hashlib.sha256(
            (source_document_id + "|" + canonical_text).encode("utf-8")
        ).hexdigest()[:24]
        run_id = "macro-slice:" + hashlib.sha256(
            f"{source_document_id}|{research_id}".encode("utf-8")
        ).hexdigest()[:24]
        evidence = make_evidence(
            "macro",
            source="macro_cache",
            source_ref=source.source_ref or f"macro:{asof}",
            title="跨市場 macro 快取",
            url=source.url,
            observed_at=asof,
            as_of=asof,
            retrieved_at=source.retrieved_at,
            excerpt=macro_evidence_excerpt(data),
            quality_status=quality_status,
            source_document_id=source_document_id,
            provenance_status="exact",
            metadata={
                "pipeline": PIPELINE,
                "source_dates": dict(data.get("source_dates") or raw.get("source_dates") or {}),
                "temporal_status": data.get("source_date_status") or raw.get("source_date_status") or "unknown",
                "quality_status": quality_status,
            },
        )
        # 研究切片可能是在品質判定規則加入前建立；重跑同一 exact raw
        # 時同步修正 manifest 與 evidence，不改 raw bytes，也不造新來源。
        if source.quality_status != quality_status:
            source.quality_status = quality_status
            try:
                source_metadata = json.loads(source.metadata_json or "{}")
            except json.JSONDecodeError:
                source_metadata = {}
            source_metadata["quality_status"] = quality_status
            source_metadata["source_date_status"] = quality_input.get(
                "source_date_status", "unknown"
            )
            source.metadata_json = json.dumps(
                source_metadata, ensure_ascii=False, sort_keys=True, default=str,
            )
            database.upsert_source_document(source)
        persist_evidence_items(database, [evidence])
        claim_specs = [
            ("asof_date", summary["asof_date"], raw.get("asof_date") == summary["asof_date"]),
            ("source_date_status", summary["source_date_status"], raw.get("source_date_status") == summary["source_date_status"]),
            ("indices_count", summary["indices_count"], len(raw.get("indices") or {}) == summary["indices_count"]),
            ("stocks_count", summary["stocks_count"], len(raw.get("stocks") or {}) == summary["stocks_count"]),
            ("adr_premiums_count", summary["adr_premiums_count"], len(raw.get("adr_premiums") or []) == summary["adr_premiums_count"]),
        ]
        claim_ids = [f"{run_id}:claim:{index}" for index, _ in enumerate(claim_specs)]
        existing = (
            database.get_research_document(research_id) is not None
            and database.get_llm_run(run_id) is not None
            and database.get_evidence(str(evidence["id"])) is not None
            and all(database.get_claim(claim_id) is not None for claim_id in claim_ids)
        )
        if existing:
            return MacroSnapshotSlice(
                source_document_id=source_document_id,
                research_document_id=research_id,
                evidence_id=str(evidence["id"]),
                run_id=run_id,
                claim_ids=claim_ids,
                chunk_ids=[chunk.chunk_id for chunk in database.list_research_chunks(research_id)],
                status="reused",
            )

        registry = SourceRegistry(project_root, db=database)
        research = registry.materialize_research_document(
            source_document_id,
            canonical_text,
            document_type="macro_snapshot",
            title=source.title or "Market macro snapshot",
            published_at=source.published_at or asof,
            metadata={"pipeline": PIPELINE, "summary": summary},
        )
        now = now_tw().isoformat(timespec="seconds")
        database.upsert_llm_run(LlmRunRow(
            run_id=run_id,
            task_type=INGEST_TASK_TYPE,
            model="",
            prompt_id=PIPELINE,
            prompt_version="1.0",
            started_at=now,
            finished_at=now,
            status=INGEST_RUN_STATUS,
            input_hash=hashlib.sha256(source_document_id.encode("utf-8")).hexdigest(),
            output_hash=hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
            trace_id=source_document_id,
            metadata_json=json.dumps({
                "pipeline": PIPELINE,
                "source_document_id": source_document_id,
                "research_document_id": research.document_id,
                "run_kind": "ingest",
            }, ensure_ascii=False, sort_keys=True),
        ))
        database.link_llm_run_evidence(LlmRunEvidenceRow(
            run_id=run_id,
            evidence_id=str(evidence["id"]),
            ordinal=0,
            role="supporting",
        ))
        for index, (field_name, value, supported) in enumerate(claim_specs):
            claim_id = claim_ids[index]
            status = "verified" if supported else "unverified"
            database.upsert_claim(ClaimRow(
                claim_id=claim_id,
                run_id=run_id,
                text=f"macro snapshot {field_name}={value}",
                claim_type="source_fact",
                confidence=1.0 if supported else 0.0,
                status=status,
                uncertainty="" if supported else "欄位值無法在 raw snapshot 核對",
                created_at=now,
                updated_at=now,
            ))
            database.link_claim_evidence(ClaimEvidenceRow(
                claim_id=claim_id,
                evidence_id=str(evidence["id"]),
                ordinal=0,
            ))
        return MacroSnapshotSlice(
            source_document_id=source_document_id,
            research_document_id=research.document_id,
            evidence_id=str(evidence["id"]),
            run_id=run_id,
            claim_ids=claim_ids,
            chunk_ids=[chunk.chunk_id for chunk in database.list_research_chunks(research.document_id)],
            status="ok",
        )
    except Exception as exc:
        return MacroSnapshotSlice(
            source_document_id=str(source_document_id or ""),
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_db:
            database.close()


__all__ = [
    "INGEST_RUN_STATUS",
    "INGEST_TASK_TYPE",
    "MacroSnapshotSlice",
    "macro_evidence_excerpt",
    "materialize_macro_snapshot_slice",
]

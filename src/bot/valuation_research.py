"""每日估值（PER/PBR/殖利率）的可追溯 research/evidence slice。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.evidence import make_evidence, persist_evidence_items
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.stock_db import (
    ClaimEvidenceRow,
    ClaimRow,
    LlmRunEvidenceRow,
    LlmRunRow,
    StockDB,
    ValuationDaily,
)
from bot.utils import now_tw

PIPELINE = "valuation_daily_vertical_slice"
SOURCE_NAME = "valuation_daily"
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class ValuationSlice:
    source_document_id: str
    symbol: str = ""
    as_of: str = ""
    research_document_id: str = ""
    evidence_id: str = ""
    claim_ids: List[str] = field(default_factory=list)
    run_id: str = ""
    canonical_json: str = ""
    chunk_ids: List[str] = field(default_factory=list)
    status: str = "ok"
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _normal(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def _payload(row: ValuationDaily, source_document_id: str) -> Dict[str, Any]:
    return {
        "symbol": row.symbol,
        "as_of": row.as_of,
        "pe_ratio": float(row.pe_ratio or 0),
        "pb_ratio": float(row.pb_ratio or 0),
        "dividend_yield": float(row.dividend_yield or 0),
        "dividend_year": row.dividend_year,
        "source_document_id": source_document_id,
        "source_url": row.source_url,
        "source_date": row.source_date,
        "retrieved_at": row.retrieved_at,
        "quality_status": row.quality_status,
        "pipeline": PIPELINE,
    }


def _raw_ok(root: Path, db: StockDB, source_document_id: str) -> tuple[bool, str]:
    source = db.get_source_document(source_document_id)
    if source is None:
        return False, f"unknown source_document_id: {source_document_id}"
    path = resolve_source_document_path(root, source)
    if not path.is_file():
        return False, f"raw archive missing: {source.content_path}"
    if source.content_hash:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != source.content_hash:
            return False, "raw archive hash mismatch"
    if source.quality_status not in {"ok", "partial"}:
        return False, f"source quality_status={source.quality_status} 不可切片"
    return True, ""


def materialize_valuation_slice(
    *,
    root: Path,
    valuation: ValuationDaily,
    db: Optional[StockDB] = None,
) -> ValuationSlice:
    source_document_id = str(valuation.source_record_id or "")
    owns_db = db is None
    database = db or StockDB.open(root=root)
    try:
        if not source_document_id.startswith("source-doc:"):
            return ValuationSlice(
                source_document_id=source_document_id,
                symbol=valuation.symbol,
                as_of=valuation.as_of,
                status="failed",
                error="估值缺少 exact source_document_id",
            )
        ok, error = _raw_ok(Path(root), database, source_document_id)
        if not ok:
            return ValuationSlice(
                source_document_id=source_document_id,
                symbol=valuation.symbol,
                as_of=valuation.as_of,
                status="failed",
                error=error,
            )

        payload = _payload(valuation, source_document_id)
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        research = SourceRegistry(Path(root), db=database).materialize_research_document(
            source_document_id,
            text,
            document_type="valuation_daily",
            symbol=valuation.symbol,
            title=f"{valuation.symbol} 估值 {valuation.as_of}",
            # source_date 保留在 canonical／observed_at；缺發布時間時 published_at 留空，
            # 不可用 as_of 冒充來源發布時間。
            published_at=valuation.source_date or "",
            metadata={"pipeline": PIPELINE, "as_of": valuation.as_of},
        )
        research_id = research.document_id
        evidence = make_evidence(
            "valuation_daily",
            source=SOURCE_NAME,
            source_ref=f"{valuation.symbol}:{valuation.as_of}",
            title=f"{valuation.symbol} 估值 {valuation.as_of}",
            url=valuation.source_url,
            observed_at=valuation.source_date or valuation.as_of,
            as_of=valuation.as_of,
            published_at=valuation.source_date or "",
            retrieved_at=valuation.retrieved_at,
            excerpt=text[:2000],
            quality_status=valuation.quality_status,
            source_document_id=source_document_id,
            provenance_status="exact",
            metadata={"pipeline": PIPELINE, "research_document_id": research_id},
        )
        persist_evidence_items(database, [evidence])
        evidence_id = str(evidence["id"])
        run_id = "valuation-slice:" + hashlib.sha256(
            f"{source_document_id}|{valuation.symbol}|{valuation.as_of}|{text}".encode("utf-8")
        ).hexdigest()[:24]
        now = now_tw().isoformat(timespec="seconds")
        database.upsert_llm_run(LlmRunRow(
            run_id=run_id,
            task_type="valuation_daily_ingest",
            prompt_id=PIPELINE,
            prompt_version="1.0",
            started_at=now,
            finished_at=now,
            status="source_ingested",
            input_hash=hashlib.sha256(source_document_id.encode("utf-8")).hexdigest(),
            output_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            trace_id=source_document_id,
            metadata_json=json.dumps({
                "pipeline": PIPELINE,
                "source_document_id": source_document_id,
                "research_document_id": research_id,
                "symbol": valuation.symbol,
                "as_of": valuation.as_of,
                "run_kind": "ingest",
            }, ensure_ascii=False, sort_keys=True),
        ))
        database.link_llm_run_evidence(LlmRunEvidenceRow(
            run_id=run_id, evidence_id=evidence_id, ordinal=0, role="supporting",
        ))

        specs = [
            ("期間", "as_of"),
            ("PER", "pe_ratio"),
            ("PBR", "pb_ratio"),
            ("殖利率", "dividend_yield"),
        ]
        claim_ids: List[str] = []
        for ordinal, (label, key) in enumerate(specs):
            value = payload[key]
            snippet = f"{json.dumps(key, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)}"
            verified = _normal(snippet) in _normal(text)
            claim_id = f"{run_id}:claim:{ordinal}"
            database.upsert_claim(ClaimRow(
                claim_id=claim_id,
                run_id=run_id,
                text=f"{valuation.symbol} {valuation.as_of} {label}為 {value}",
                claim_type="source_fact",
                confidence=1.0 if verified else 0.0,
                status="verified" if verified else "unverified",
                uncertainty="" if verified else "欄位無法在 canonical JSON 核對",
                created_at=now,
                updated_at=now,
            ))
            database.link_claim_evidence(ClaimEvidenceRow(
                claim_id=claim_id, evidence_id=evidence_id, ordinal=ordinal,
            ))
            claim_ids.append(claim_id)

        return ValuationSlice(
            source_document_id=source_document_id,
            symbol=valuation.symbol,
            as_of=valuation.as_of,
            research_document_id=research_id,
            evidence_id=evidence_id,
            claim_ids=claim_ids,
            run_id=run_id,
            canonical_json=text,
            chunk_ids=[c.chunk_id for c in database.list_research_chunks(research_id)],
        )
    except Exception as exc:
        return ValuationSlice(
            source_document_id=source_document_id,
            symbol=valuation.symbol,
            as_of=valuation.as_of,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_db:
            database.close()


__all__ = ["ValuationSlice", "materialize_valuation_slice"]

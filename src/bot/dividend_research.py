"""年度股利分派的 canonical/research/evidence 垂直切片。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.evidence import make_evidence, persist_evidence_items
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.stock_db import (
    ClaimEvidenceRow,
    ClaimRow,
    DividendHistory,
    LlmRunEvidenceRow,
    LlmRunRow,
    StockDB,
)
from bot.utils import now_tw

PIPELINE = "dividend_history_vertical_slice"


@dataclass
class DividendSlice:
    source_document_id: str
    symbol: str = ""
    year: int = 0
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


def materialize_dividend_slice(
    *,
    root: Path,
    dividend: DividendHistory,
    db: Optional[StockDB] = None,
) -> DividendSlice:
    source_document_id = str(dividend.source_record_id or "")
    owns_db = db is None
    database = db or StockDB.open(root=root)
    try:
        if not source_document_id.startswith("source-doc:"):
            return DividendSlice(
                source_document_id=source_document_id,
                symbol=dividend.symbol,
                year=dividend.year,
                status="failed",
                error="股利缺少 exact source_document_id",
            )
        ok, error = _raw_ok(Path(root), database, source_document_id)
        if not ok:
            return DividendSlice(
                source_document_id=source_document_id,
                symbol=dividend.symbol,
                year=dividend.year,
                status="failed",
                error=error,
            )

        payload = {
            "symbol": dividend.symbol,
            "year": dividend.year,
            "cash_dividend": float(dividend.cash_dividend or 0),
            "stock_dividend": float(dividend.stock_dividend or 0),
            "ex_dividend_date": dividend.ex_dividend_date,
            "ex_right_date": dividend.ex_right_date,
            "fill_date": dividend.fill_date,
            "fill_days": dividend.fill_days,
            "payout_ratio": dividend.payout_ratio,
            "source_document_id": source_document_id,
            "source_url": dividend.source_url,
            "source_date": dividend.source_date,
            "retrieved_at": dividend.retrieved_at,
            "quality_status": dividend.quality_status,
            "pipeline": PIPELINE,
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        research = SourceRegistry(Path(root), db=database).materialize_research_document(
            source_document_id,
            text,
            document_type="dividend_history",
            symbol=dividend.symbol,
            title=f"{dividend.symbol} 股利 {dividend.year}",
            # 股利年度是 as_of 期間；缺真實發布日時 published_at 留空。
            published_at=dividend.source_date or "",
            metadata={"pipeline": PIPELINE, "year": dividend.year},
        )
        research_id = research.document_id
        evidence = make_evidence(
            "dividend_history",
            source="dividend_history",
            source_ref=f"{dividend.symbol}:{dividend.year}",
            title=f"{dividend.symbol} 股利 {dividend.year}",
            url=dividend.source_url,
            observed_at=dividend.source_date or str(dividend.year),
            as_of=str(dividend.year),
            published_at=dividend.source_date or "",
            retrieved_at=dividend.retrieved_at,
            excerpt=text[:2000],
            quality_status=dividend.quality_status,
            source_document_id=source_document_id,
            provenance_status="exact",
            metadata={"pipeline": PIPELINE, "research_document_id": research_id},
        )
        persist_evidence_items(database, [evidence])
        evidence_id = str(evidence["id"])
        run_id = "dividend-slice:" + hashlib.sha256(
            f"{source_document_id}|{dividend.symbol}|{dividend.year}|{text}".encode("utf-8")
        ).hexdigest()[:24]
        now = now_tw().isoformat(timespec="seconds")
        database.upsert_llm_run(LlmRunRow(
            run_id=run_id,
            task_type="dividend_history_ingest",
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
                "symbol": dividend.symbol,
                "year": dividend.year,
                "run_kind": "ingest",
            }, ensure_ascii=False, sort_keys=True),
        ))
        database.link_llm_run_evidence(LlmRunEvidenceRow(
            run_id=run_id, evidence_id=evidence_id, ordinal=0, role="supporting",
        ))

        specs = [
            ("現金股利", "cash_dividend"),
            ("股票股利", "stock_dividend"),
            ("除息日", "ex_dividend_date"),
            ("除權日", "ex_right_date"),
        ]
        claim_ids: List[str] = []
        for ordinal, (label, key) in enumerate(specs):
            value = payload[key]
            snippet = f"{json.dumps(key, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)}"
            verified = snippet in text
            claim_id = f"{run_id}:claim:{ordinal}"
            database.upsert_claim(ClaimRow(
                claim_id=claim_id,
                run_id=run_id,
                text=f"{dividend.symbol} {dividend.year} {label}為 {value}",
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

        return DividendSlice(
            source_document_id=source_document_id,
            symbol=dividend.symbol,
            year=dividend.year,
            research_document_id=research_id,
            evidence_id=evidence_id,
            claim_ids=claim_ids,
            run_id=run_id,
            canonical_json=text,
            chunk_ids=[c.chunk_id for c in database.list_research_chunks(research_id)],
        )
    except Exception as exc:
        return DividendSlice(
            source_document_id=source_document_id,
            symbol=dividend.symbol,
            year=dividend.year,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_db:
            database.close()


__all__ = ["DividendSlice", "materialize_dividend_slice"]

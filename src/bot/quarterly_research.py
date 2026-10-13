"""季報 EPS／三率的 raw -> canonical -> evidence 垂直切片。

季報原始資料是年度累計後再由 ``fundamentals_fetcher`` 解累計成單季；本模組
只保存可由該 canonical row 直接核對的 source facts，不產生看多／看空結論，
也不依 URL、日期或文字相似度猜測 ``source_document_id``。
"""

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
    QuarterlyReport,
    StockDB,
)
from bot.utils import now_tw

PIPELINE = "quarterly_financials_vertical_slice"
SOURCE_NAME = "quarterly_financials"
INGEST_TASK_TYPE = "quarterly_financials_ingest"
INGEST_RUN_STATUS = "source_ingested"
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class QuarterlySlice:
    source_document_id: str
    symbol: str = ""
    period: str = ""
    research_document_id: str = ""
    evidence_id: str = ""
    claim_ids: List[str] = field(default_factory=list)
    run_id: str = ""
    canonical_json: str = ""
    chunk_ids: List[str] = field(default_factory=list)
    provenance_status: str = "exact"
    status: str = "ok"
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _normalize(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip()


def _supported(snippet: str, text: str) -> bool:
    return bool(_normalize(snippet)) and _normalize(snippet) in _normalize(text)


def _number(value: float) -> float:
    return float(value or 0.0)


def _payload(report: QuarterlyReport, source_document_id: str) -> Dict[str, Any]:
    return {
        "symbol": report.symbol,
        "period": report.period,
        "eps": _number(report.eps),
        "revenue": _number(report.revenue),
        "gross_margin": _number(report.gross_margin),
        "op_margin": _number(report.op_margin),
        "net_margin": _number(report.net_margin),
        "source_document_id": source_document_id,
        "source_url": report.source_url,
        "source_date": report.source_date,
        "retrieved_at": report.retrieved_at,
        "quality_status": report.quality_status,
        "pipeline": PIPELINE,
        "field_defs": {
            "period": "會計年度與季度，例如 2026Q2",
            "eps": "每股盈餘",
            "revenue": "季營收；來源原始單位依 API 定義",
            "gross_margin": "毛利率 %",
            "op_margin": "營業利益率 %",
            "net_margin": "淨利率 %",
        },
    }


def _claims(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    def fragment(key: str) -> str:
        return f"{json.dumps(key, ensure_ascii=False)}: {json.dumps(payload[key], ensure_ascii=False)}"

    symbol = str(payload["symbol"])
    period = str(payload["period"])
    return [
        {"text": f"{symbol} 季報期間為 {period}", "snippet": fragment("period")},
        {"text": f"{symbol} {period} EPS 為 {payload['eps']}", "snippet": fragment("eps")},
        {"text": f"{symbol} {period} 季營收為 {payload['revenue']}", "snippet": fragment("revenue")},
        {"text": f"{symbol} {period} 毛利率為 {payload['gross_margin']}%", "snippet": fragment("gross_margin")},
        {"text": f"{symbol} {period} 營業利益率為 {payload['op_margin']}%", "snippet": fragment("op_margin")},
        {"text": f"{symbol} {period} 淨利率為 {payload['net_margin']}%", "snippet": fragment("net_margin")},
    ]


def _raw_is_replayable(root: Path, db: StockDB, source_document_id: str) -> tuple[bool, str]:
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


def materialize_quarterly_slice(
    *,
    root: Path,
    report: QuarterlyReport,
    db: Optional[StockDB] = None,
) -> QuarterlySlice:
    """將一筆 exact canonical 季報建立成 research/evidence/claim。"""
    project_root = Path(root)
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    source_document_id = str(report.source_record_id or "")
    try:
        if not source_document_id.startswith("source-doc:"):
            return QuarterlySlice(
                source_document_id=source_document_id,
                symbol=report.symbol,
                period=report.period,
                provenance_status="unresolved",
                status="failed",
                error="季報缺少 exact source_document_id",
            )
        replayable, error = _raw_is_replayable(project_root, database, source_document_id)
        if not replayable:
            return QuarterlySlice(
                source_document_id=source_document_id,
                symbol=report.symbol,
                period=report.period,
                status="failed",
                error=error,
            )

        payload = _payload(report, source_document_id)
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        research_id = "research-doc:" + hashlib.sha256(
            f"{source_document_id}|{report.symbol}|{report.period}|{text}".encode("utf-8")
        ).hexdigest()[:24]
        registry = SourceRegistry(project_root, db=database)
        research = registry.materialize_research_document(
            source_document_id,
            text,
            document_type="quarterly_report",
            symbol=report.symbol,
            title=f"{report.symbol} 季報 {report.period}",
            # 季報 upstream 通常只有所屬期間；不可把 period 冒充 published_at。
            published_at="",
            metadata={"pipeline": PIPELINE, "period": report.period},
        )
        research_id = research.document_id

        evidence = make_evidence(
            "quarterly_report",
            source=SOURCE_NAME,
            source_ref=f"{report.symbol}:{report.period}",
            title=f"{report.symbol} 季報 {report.period}",
            url=report.source_url,
            observed_at=report.source_date or report.period,
            as_of=report.period,
            published_at="",
            retrieved_at=report.retrieved_at,
            excerpt=text[:2000],
            quality_status=report.quality_status,
            source_document_id=source_document_id,
            provenance_status="exact",
            metadata={
                "pipeline": PIPELINE,
                "research_document_id": research_id,
                "symbol": report.symbol,
                "period": report.period,
            },
        )
        persist_evidence_items(database, [evidence])
        evidence_id = str(evidence["id"])

        run_id = "quarterly-slice:" + hashlib.sha256(
            f"{source_document_id}|{report.symbol}|{report.period}|{text}".encode("utf-8")
        ).hexdigest()[:24]
        now = now_tw().isoformat(timespec="seconds")
        database.upsert_llm_run(LlmRunRow(
            run_id=run_id,
            task_type=INGEST_TASK_TYPE,
            prompt_id=PIPELINE,
            prompt_version="1.0",
            started_at=now,
            finished_at=now,
            status=INGEST_RUN_STATUS,
            input_hash=hashlib.sha256(source_document_id.encode("utf-8")).hexdigest(),
            output_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            trace_id=source_document_id,
            metadata_json=json.dumps({
                "pipeline": PIPELINE,
                "source_document_id": source_document_id,
                "research_document_id": research_id,
                "symbol": report.symbol,
                "period": report.period,
                "run_kind": "ingest",
            }, ensure_ascii=False, sort_keys=True),
        ))
        database.link_llm_run_evidence(LlmRunEvidenceRow(
            run_id=run_id, evidence_id=evidence_id, ordinal=0, role="supporting",
        ))

        claim_ids: List[str] = []
        for ordinal, spec in enumerate(_claims(payload)):
            claim_id = f"{run_id}:claim:{ordinal}"
            verified = _supported(spec["snippet"], text)
            database.upsert_claim(ClaimRow(
                claim_id=claim_id,
                run_id=run_id,
                text=spec["text"],
                claim_type="source_fact",
                confidence=1.0 if verified else 0.0,
                status="verified" if verified else "unverified",
                uncertainty="" if verified else "欄位無法在 canonical JSON 核對",
                created_at=now,
                updated_at=now,
            ))
            database.link_claim_evidence(ClaimEvidenceRow(
                claim_id=claim_id,
                evidence_id=evidence_id,
                relation="supports",
                ordinal=ordinal,
            ))
            claim_ids.append(claim_id)

        return QuarterlySlice(
            source_document_id=source_document_id,
            symbol=report.symbol,
            period=report.period,
            research_document_id=research_id,
            evidence_id=evidence_id,
            claim_ids=claim_ids,
            run_id=run_id,
            canonical_json=text,
            chunk_ids=[c.chunk_id for c in database.list_research_chunks(research_id)],
            status="ok",
        )
    except Exception as exc:
        return QuarterlySlice(
            source_document_id=source_document_id,
            symbol=report.symbol,
            period=report.period,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if owns_db:
            database.close()


__all__ = ["QuarterlySlice", "materialize_quarterly_slice"]

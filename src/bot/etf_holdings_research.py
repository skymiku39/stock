"""ETF 持股的 raw -> research -> evidence -> claim 垂直切片。

ETF 持股可能是確定性 HTML 解析，也可能是 LLM 從 HTML/PDF 抽取；兩者都
必須保留同一個 exact ``source_document_id``，但「抽取結果」不能自動等同
於原文已證明。每筆 claim 只有在持股識別、權重都能在 immutable raw archive
核對時才標成 ``verified``，否則保留為 ``unverified`` 並帶出不確定性。

這個模組不依 URL、檔名、ETF 名稱或相似度猜測來源。呼叫端必須傳入已存在
的 exact source document；CSV 仍是既有 canonical holdings storage，research
layer 只是可重建的衍生層。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.active_etf import HoldingsSnapshot, Holding
from bot.etf_holdings_validator import validate_etf_holdings
from bot.evidence import make_evidence, persist_evidence_items
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.stock_db import (
    ClaimEvidenceRow,
    ClaimRow,
    LlmRunEvidenceRow,
    LlmRunRow,
    StockDB,
)
from bot.utils import now_tw


PIPELINE = "etf_holdings_vertical_slice"
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class EtfHoldingsSlice:
    source_document_id: str
    symbol: str = ""
    as_of: str = ""
    research_document_id: str = ""
    evidence_id: str = ""
    claim_ids: List[str] = field(default_factory=list)
    run_id: str = ""
    canonical_json: str = ""
    chunk_ids: List[str] = field(default_factory=list)
    status: str = "ok"  # ok / reused / failed / empty
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _normal(value: Any) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "")).strip().casefold()


def _source_metadata(source: Any) -> Dict[str, Any]:
    try:
        value = json.loads(str(source.metadata_json or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _read_raw_text(root: Path, source: Any) -> tuple[bool, str]:
    path = resolve_source_document_path(root, source)
    if not path.is_file():
        return False, f"raw archive missing: {source.content_path}"
    try:
        payload = path.read_bytes()
    except OSError as exc:
        return False, f"raw archive unreadable: {exc}"
    expected = str(source.content_hash or "")
    if expected and hashlib.sha256(payload).hexdigest() != expected:
        return False, "raw archive hash mismatch"

    # PDF extraction is optional.  If it is unavailable, the source remains
    # usable for provenance but claims conservatively stay unverified.
    if str(source.mime_type or "").lower().find("pdf") >= 0 or path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(io.BytesIO(payload))
            text = "\n\n".join(
                page.extract_text() or "" for page in reader.pages[:60]
            )
            return True, text
        except Exception:
            return True, ""
    return True, payload.decode("utf-8", errors="replace")


def _number_forms(value: Any) -> List[str]:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return []
    forms = {
        str(value).strip(),
        f"{number:g}",
        f"{number:.2f}",
        f"{number:.4f}".rstrip("0").rstrip("."),
    }
    return [form.casefold() for form in forms if form]


def _holding_supported(holding: Holding, raw_text: str) -> bool:
    raw = _normal(raw_text)
    identities = [
        _normal(holding.ticker),
        _normal(holding.name),
    ]
    identities = [value for value in identities if value]
    if not identities or not any(value in raw for value in identities):
        return False
    weights = _number_forms(holding.weight_pct)
    return bool(weights) and any(weight in raw for weight in weights)


def _canonical_payload(
    snapshot: HoldingsSnapshot,
    source_document_id: str,
) -> Dict[str, Any]:
    return {
        "symbol": snapshot.symbol,
        "as_of": snapshot.date.isoformat(),
        "source_document_id": source_document_id,
        "holdings": [asdict(holding) for holding in snapshot.holdings],
        "pipeline": PIPELINE,
    }


def materialize_etf_holdings_slice(
    *,
    root: Path,
    snapshot: HoldingsSnapshot,
    source_document_id: str,
    db: Optional[StockDB] = None,
) -> EtfHoldingsSlice:
    """由 exact raw manifest 與已保存的 canonical snapshot 建立研究鏈。"""
    project_root = Path(root)
    source_id = str(source_document_id or "").strip()
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    base = EtfHoldingsSlice(
        source_document_id=source_id,
        symbol=str(snapshot.symbol or ""),
        as_of=snapshot.date.isoformat(),
    )
    try:
        if not source_id.startswith("source-doc:"):
            base.status = "failed"
            base.error = "ETF 持股缺少 exact source_document_id"
            return base
        source = database.get_source_document(source_id)
        if source is None:
            base.status = "failed"
            base.error = f"unknown source_document_id: {source_id}"
            return base
        valid, raw_text = _read_raw_text(project_root, source)
        if not valid:
            base.status = "failed"
            base.error = raw_text
            return base
        if not snapshot.holdings:
            base.status = "empty"
            base.error = "ETF 持股快照沒有資料"
            return base

        validation = validate_etf_holdings(
            snapshot,
            as_of=base.as_of,
            retrieved_at=str(source.retrieved_at or ""),
            source_quality_status=str(source.quality_status or ""),
        )
        if not validation.usable:
            base.status = "failed"
            base.error = "; ".join(validation.errors) or "ETF 持股驗證失敗"
            return base

        payload = _canonical_payload(snapshot, source_id)
        canonical_text = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2,
        )
        research_id = "research-doc:" + hashlib.sha256(
            (source_id + "|" + canonical_text).encode("utf-8")
        ).hexdigest()[:24]
        run_id = "etf-holdings-slice:" + hashlib.sha256(
            (source_id + "|" + canonical_text).encode("utf-8")
        ).hexdigest()[:24]
        evidence = make_evidence(
            "etf_holdings",
            source=str(source.source or "etf_holdings"),
            source_ref=str(source.source_ref or f"{snapshot.symbol}:{base.as_of}"),
            title=f"{snapshot.symbol} ETF 持股 {base.as_of}",
            url=str(source.url or ""),
            observed_at=base.as_of,
            as_of=base.as_of,
            published_at=str(source.published_at or source.as_of or base.as_of),
            retrieved_at=str(source.retrieved_at or ""),
            excerpt=canonical_text[:2000],
            quality_status=validation.quality_status,
            source_document_id=source_id,
            provenance_status="exact",
            metadata={
                "pipeline": PIPELINE,
                "research_document_id": research_id,
                "extraction_method": _source_metadata(source).get(
                    "extraction_method", "canonical_holdings_snapshot",
                ),
                "holding_count": len(snapshot.holdings),
                "raw_text_available": bool(raw_text.strip()),
                "validation": {
                    "quality_status": validation.quality_status,
                    "errors": list(validation.errors),
                    "warnings": list(validation.warnings),
                    "weight_sum": validation.weight_sum,
                    "duplicate_tickers": list(validation.duplicate_tickers),
                },
            },
        )
        claim_specs: List[tuple[str, str, bool]] = []
        for ordinal, holding in enumerate(snapshot.holdings):
            supported = _holding_supported(holding, raw_text)
            claim_specs.append((
                f"{run_id}:claim:{ordinal}",
                (
                    f"{snapshot.symbol} 於 {base.as_of} 持有 "
                    f"{holding.ticker or holding.name}，權重 {holding.weight_pct:g}%"
                ),
                supported,
            ))

        existing = (
            database.get_research_document(research_id) is not None
            and database.get_llm_run(run_id) is not None
            and database.get_evidence(str(evidence["id"])) is not None
            and all(database.get_claim(claim_id) is not None for claim_id, _, _ in claim_specs)
        )
        if existing:
            base.status = "reused"
            base.research_document_id = research_id
            base.evidence_id = str(evidence["id"])
            base.run_id = run_id
            base.claim_ids = [claim_id for claim_id, _, _ in claim_specs]
            base.canonical_json = canonical_text
            base.chunk_ids = [
                chunk.chunk_id
                for chunk in database.list_research_chunks(research_id)
            ]
            return base

        research = SourceRegistry(project_root, db=database).materialize_research_document(
            source_id,
            canonical_text,
            document_type="etf_holdings",
            symbol=snapshot.symbol,
            title=f"{snapshot.symbol} ETF 持股 {base.as_of}",
            published_at=str(source.published_at or source.as_of or base.as_of),
            metadata={
                "pipeline": PIPELINE,
                "holding_count": len(snapshot.holdings),
                "source_document_id": source_id,
            },
        )
        persist_evidence_items(database, [evidence])
        now = now_tw().isoformat(timespec="seconds")
        database.upsert_llm_run(LlmRunRow(
            run_id=run_id,
            task_type="etf_holdings_ingest",
            model="",
            prompt_id=PIPELINE,
            prompt_version="1.0",
            started_at=now,
            finished_at=now,
            status="source_ingested",
            input_hash=hashlib.sha256(source_id.encode("utf-8")).hexdigest(),
            output_hash=hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
            trace_id=source_id,
            metadata_json=json.dumps({
                "pipeline": PIPELINE,
                "source_document_id": source_id,
                "research_document_id": research.document_id,
                "symbol": snapshot.symbol,
                "as_of": base.as_of,
                "run_kind": "ingest",
            }, ensure_ascii=False, sort_keys=True),
        ))
        evidence_id = str(evidence["id"])
        claim_ids: List[str] = []
        for ordinal, (claim_id, claim_text, supported) in enumerate(claim_specs):
            database.link_llm_run_evidence(LlmRunEvidenceRow(
                run_id=run_id,
                evidence_id=evidence_id,
                ordinal=ordinal,
                role="supporting",
            ))
            database.upsert_claim(ClaimRow(
                claim_id=claim_id,
                run_id=run_id,
                text=claim_text,
                claim_type="source_excerpt" if supported else "derived_fact",
                confidence=1.0 if supported else 0.0,
                status="verified" if supported else "unverified",
                uncertainty="" if supported else "抽取持股／權重無法在 raw archive 完整核對",
                created_at=now,
                updated_at=now,
            ))
            database.link_claim_evidence(ClaimEvidenceRow(
                claim_id=claim_id,
                evidence_id=evidence_id,
                ordinal=ordinal,
            ))
            claim_ids.append(claim_id)

        return EtfHoldingsSlice(
            source_document_id=source_id,
            symbol=snapshot.symbol,
            as_of=base.as_of,
            research_document_id=research.document_id,
            evidence_id=evidence_id,
            claim_ids=claim_ids,
            run_id=run_id,
            canonical_json=canonical_text,
            chunk_ids=[
                chunk.chunk_id
                for chunk in database.list_research_chunks(research.document_id)
            ],
        )
    except Exception as exc:  # noqa: BLE001
        base.status = "failed"
        base.error = f"{type(exc).__name__}: {exc}"
        return base
    finally:
        if owns_db:
            database.close()


__all__ = ["EtfHoldingsSlice", "PIPELINE", "materialize_etf_holdings_slice"]

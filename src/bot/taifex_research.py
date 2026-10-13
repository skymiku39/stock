"""TAIFEX TX 日行情的 raw/research/evidence/claim 垂直切片。

這個模組只 materialize 呼叫端已提供的 exact ``source_document_id``。
TAIFEX 原始文件只支持期貨欄位；現貨與正逆價差是外部輸入／計算結果，
因此不會被標記成 TAIFEX 原文事實。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

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

PIPELINE = "taifex_tx_research_slice"


@dataclass
class TaifexSlice:
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


def _number(value: Any) -> float:
    try:
        text = str(value or "").replace(",", "").strip()
        if not text or text in {"-", "--"}:
            return 0.0
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _date_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())[:8]


def _same_number(left: Any, right: Any) -> bool:
    a, b = _number(left), _number(right)
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


def _normalize_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "contract": row.get("Contract") or row.get("契約代號") or "",
        "contract_month": (
            row.get("ContractMonth(Week)")
            or row.get("ContractMonth")
            or row.get("到期月份(週別)")
            or ""
        ),
        "last": row.get("Last") or row.get("最後成交價") or "",
        "settlement": row.get("SettlementPrice") or row.get("結算價") or "",
        "open_interest": row.get("OpenInterest") or row.get("未沖銷契約數") or "",
        "date": row.get("Date") or row.get("日期") or "",
    }


def _parse_rows(raw: bytes) -> List[Dict[str, Any]]:
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        payload = json.loads(text)
        if isinstance(payload, list):
            return [_normalize_row(row) for row in payload if isinstance(row, Mapping)]
        if isinstance(payload, Mapping):
            for key in ("data", "Data", "result", "Result"):
                rows = payload.get(key)
                if isinstance(rows, list):
                    return [_normalize_row(row) for row in rows if isinstance(row, Mapping)]
            return [_normalize_row(payload)]
    except json.JSONDecodeError:
        pass
    reader = csv.DictReader(io.StringIO(text))
    return [_normalize_row(row) for row in reader if row]


def _load_exact_raw(root: Path, database: StockDB, source_document_id: str) -> tuple[Any, bytes]:
    source = database.get_source_document(source_document_id)
    if source is None:
        raise ValueError(f"unknown source_document_id: {source_document_id}")
    if not str(source_document_id).startswith("source-doc:"):
        raise ValueError("source_document_id 不是 exact source-doc identity")
    path = resolve_source_document_path(root, source)
    if not path.is_file():
        raise FileNotFoundError(f"raw archive 不存在: {source.content_path}")
    content = path.read_bytes()
    if source.content_hash and hashlib.sha256(content).hexdigest() != source.content_hash:
        raise ValueError("raw archive hash mismatch")
    if source.quality_status not in {"ok", "partial"}:
        raise ValueError(f"source quality_status={source.quality_status} 不可切片")
    return source, content


def materialize_taifex_slice(
    *,
    root: Path,
    source_document_id: str,
    contract: str = "TX",
    contract_month: str = "",
    futures_price: float = 0.0,
    spot_price: float = 0.0,
    basis: float = 0.0,
    basis_pct: float = 0.0,
    open_interest: float = 0.0,
    state: str = "",
    asof_date: str = "",
    db: Optional[StockDB] = None,
) -> TaifexSlice:
    """從 exact TAIFEX raw archive 建立研究文件、證據與可核對 claims。"""
    project_root = Path(root)
    owns_db = db is None
    database = db or StockDB.open(root=project_root)
    result = TaifexSlice(source_document_id=str(source_document_id or ""))
    try:
        source, raw_bytes = _load_exact_raw(project_root, database, result.source_document_id)
        rows = _parse_rows(raw_bytes)
        requested_month = str(contract_month or "").strip()
        candidates = [
            row for row in rows
            if str(row.get("contract") or "").strip().upper() == str(contract or "TX").upper()
            and (not requested_month or str(row.get("contract_month") or "").strip() == requested_month)
        ]
        if not candidates:
            result.status = "empty"
            result.error = "raw archive 找不到指定 TX 合約月份"
            return result
        row = candidates[0]
        month = str(row.get("contract_month") or requested_month).strip()
        observed_date = str(row.get("date") or asof_date or source.as_of).strip()
        raw_last = _number(row.get("last"))
        raw_settlement = _number(row.get("settlement"))
        raw_futures = raw_last or raw_settlement
        raw_oi = _number(row.get("open_interest"))
        canonical = {
            "contract": str(row.get("contract") or contract).strip(),
            "contract_month": month,
            "asof_date": observed_date,
            "last": raw_last,
            "settlement": raw_settlement,
            "open_interest": raw_oi,
            "calculated": {
                "futures_price": float(futures_price),
                "spot_price": float(spot_price),
                "basis": float(basis),
                "basis_pct": float(basis_pct),
                "state": str(state or "unknown"),
                "note": "calculated with supplied spot_price; not a TAIFEX raw field",
            },
            "source_document_id": result.source_document_id,
            "pipeline": PIPELINE,
        }
        canonical_text = json.dumps(canonical, ensure_ascii=False, sort_keys=True, indent=2)
        research_id = "research-doc:" + hashlib.sha256(
            (result.source_document_id + "|" + canonical_text).encode("utf-8")
        ).hexdigest()[:24]
        run_id = "taifex-slice:" + hashlib.sha256(
            f"{result.source_document_id}|{research_id}".encode("utf-8")
        ).hexdigest()[:24]
        excerpt = (
            f"TX {month} 日期={observed_date} 最後成交價={raw_last:g} "
            f"結算價={raw_settlement:g} 未沖銷契約數={raw_oi:g}"
        )[:2000]
        evidence = make_evidence(
            "taifex_futures",
            source="taifex_daily_fut",
            source_ref=source.source_ref or f"taifex:{observed_date}:{month}",
            title=source.title or f"TAIFEX TX daily futures {observed_date}",
            url=source.url,
            observed_at=observed_date,
            as_of=observed_date,
            retrieved_at=source.retrieved_at,
            excerpt=excerpt,
            quality_status=source.quality_status or "ok",
            source_document_id=result.source_document_id,
            provenance_status="exact",
            metadata={
                "pipeline": PIPELINE,
                "contract": str(row.get("contract") or contract),
                "contract_month": month,
                "derived_fields": ["futures_price", "spot_price", "basis", "basis_pct", "state"],
                "derived_values": {
                    "futures_price": float(futures_price),
                    "spot_price": float(spot_price),
                    "basis": float(basis),
                    "basis_pct": float(basis_pct),
                    "state": str(state or "unknown"),
                },
            },
        )
        evidence_id = str(evidence["id"])
        claim_specs = [
            (
                "contract",
                f"TAIFEX 合約代號為 {canonical['contract']}",
                str(row.get("contract") or "").strip().upper() == str(contract or "TX").upper(),
            ),
            (
                "contract_month",
                f"TAIFEX TX 到期月份為 {month}",
                bool(month),
            ),
            (
                "futures_price",
                f"TAIFEX TX {month} 期貨價格為 {float(futures_price):g}",
                _same_number(raw_futures, futures_price),
            ),
            (
                "open_interest",
                f"TAIFEX TX {month} 未沖銷契約數為 {raw_oi:g}",
                _same_number(raw_oi, open_interest),
            ),
            (
                "asof_date",
                f"TAIFEX TX 資料日期為 {observed_date}",
                not asof_date or _date_key(observed_date) == _date_key(asof_date),
            ),
        ]
        claim_ids = [f"{run_id}:claim:{index}" for index, _ in enumerate(claim_specs)]
        existing = (
            database.get_research_document(research_id) is not None
            and database.get_llm_run(run_id) is not None
            and database.get_evidence(evidence_id) is not None
            and all(database.get_claim(claim_id) is not None for claim_id in claim_ids)
        )
        if existing:
            return TaifexSlice(
                source_document_id=result.source_document_id,
                research_document_id=research_id,
                evidence_id=evidence_id,
                run_id=run_id,
                claim_ids=claim_ids,
                chunk_ids=[c.chunk_id for c in database.list_research_chunks(research_id)],
                status="reused",
            )

        research = SourceRegistry(project_root, db=database).materialize_research_document(
            result.source_document_id,
            canonical_text,
            document_type="taifex_futures",
            title=source.title or f"TAIFEX TX {observed_date}",
            # 行情日期留在 as_of／observed_at；缺上游發布時間時 published_at 留空。
            published_at=source.published_at or "",
            metadata={"pipeline": PIPELINE, "contract": canonical["contract"], "contract_month": month},
        )
        persist_evidence_items(database, [evidence])
        now = now_tw().isoformat(timespec="seconds")
        database.upsert_llm_run(LlmRunRow(
            run_id=run_id,
            task_type="taifex_futures_ingest",
            prompt_id=PIPELINE,
            prompt_version="1.0",
            started_at=now,
            finished_at=now,
            status="source_ingested",
            input_hash=hashlib.sha256(result.source_document_id.encode("utf-8")).hexdigest(),
            output_hash=hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
            trace_id=result.source_document_id,
            metadata_json=json.dumps({
                "pipeline": PIPELINE,
                "source_document_id": result.source_document_id,
                "research_document_id": research.document_id,
                "run_kind": "ingest",
                "derived_fields_are_not_raw_claims": True,
            }, ensure_ascii=False, sort_keys=True),
        ))
        database.link_llm_run_evidence(LlmRunEvidenceRow(
            run_id=run_id, evidence_id=evidence_id, ordinal=0, role="supporting",
        ))
        for ordinal, ((_field, claim_text, supported), claim_id) in enumerate(zip(claim_specs, claim_ids)):
            database.upsert_claim(ClaimRow(
                claim_id=claim_id,
                run_id=run_id,
                text=claim_text,
                claim_type="source_fact",
                confidence=1.0 if supported else 0.0,
                status="verified" if supported else "unverified",
                uncertainty="" if supported else "欄位值無法在 TAIFEX raw archive 核對",
                created_at=now,
                updated_at=now,
            ))
            database.link_claim_evidence(ClaimEvidenceRow(
                claim_id=claim_id, evidence_id=evidence_id, ordinal=ordinal,
            ))
        return TaifexSlice(
            source_document_id=result.source_document_id,
            research_document_id=research.document_id,
            evidence_id=evidence_id,
            run_id=run_id,
            claim_ids=claim_ids,
            chunk_ids=[c.chunk_id for c in database.list_research_chunks(research.document_id)],
            status="ok",
        )
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if owns_db:
            database.close()


__all__ = ["PIPELINE", "TaifexSlice", "materialize_taifex_slice"]

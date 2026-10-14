"""可證明資料的 LLM research-layer 回填。

回填採 exact-only policy：只使用 JSONL record 本身明確列出的 evidence ID；
不以 URL、標題、時間或文字相似度替舊資料猜來源。預設 dry-run，``apply``
才會寫入新 relational tables。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from bot.llm_log import LlmCallLogger, LlmCallRecord, _mirror_to_stock_db, research_run_id
from bot.llm_provenance import evidence_ids as normalize_evidence_ids, evidence_receipt
from bot.stock_db import (
    ClaimEvidenceRow,
    ClaimRow,
    LlmOutputRow,
    LlmRunEvidenceRow,
    LlmRunRow,
    StockDB,
    get_db,
)


def _parse_output(text: str) -> Dict[str, Any]:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        obj = json.loads(cleaned)
        return dict(obj) if isinstance(obj, dict) else {}
    except Exception:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(cleaned[start:end + 1])
                return dict(obj) if isinstance(obj, dict) else {}
            except Exception:
                pass
    return {}


def _evidence_ids(record: LlmCallRecord, parsed: Dict[str, Any]) -> list[str]:
    # Model output is not proof of which sources were supplied to the call.
    return normalize_evidence_ids((record.metadata or {}).get("evidence_ids"))


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def backfill_llm_provenance(
    *,
    root: Path,
    db: Optional[StockDB] = None,
    apply: bool = False,
    limit: int = 100_000,
) -> Dict[str, int | bool]:
    database = db or get_db(root=Path(root))
    logger = LlmCallLogger(root=Path(root))
    report: Dict[str, int | bool] = {
        "dry_run": not apply,
        "records": 0,
        "runs_upserted": 0,
        "outputs_upserted": 0,
        "claims_upserted": 0,
        "evidence_links": 0,
        "unresolved_evidence": 0,
        "unresolved_records": 0,
        "legacy_records": 0,
        "legacy_unresolved": 0,
        "legacy_runs_upserted": 0,
    }
    for index, record in enumerate(logger.iter_records()):
        if index >= limit:
            break
        report["records"] += 1
        parsed = _parse_output(record.output)
        ids = _evidence_ids(record, parsed)
        missing = [value for value in ids if database.get_evidence(value) is None]
        report["unresolved_evidence"] += len(missing)
        if evidence_receipt(database, record.metadata or {}, record.prompt_id)["state"] in {
            "missing", "partial", "unassessed",
        }:
            report["unresolved_records"] += 1
        run_id = research_run_id(record)
        if not apply:
            continue
        with database.transaction():
            _mirror_to_stock_db(record, logger=logging.getLogger(__name__), root=Path(root),
                                db=database, mirror_memory=False)
            claims = parsed.get("claims") if isinstance(parsed.get("claims"), list) else []
            for claim_index, claim in enumerate(claims):
                if not isinstance(claim, dict):
                    continue
                claim_id = f"{run_id}:claim:{claim_index}"
                claim_ids = normalize_evidence_ids(claim.get("evidence_ids"))
                database.upsert_claim(ClaimRow(
                    claim_id=claim_id, run_id=run_id,
                    text=str(claim.get("text") or claim.get("statement") or ""),
                    claim_type=str(claim.get("type") or "fact"),
                    confidence=_confidence(claim.get("confidence") or parsed.get("confidence")),
                    status="unverified",
                    uncertainty=str(claim.get("uncertainty") or ""),
                ))
                database.conn.execute("DELETE FROM claim_evidence WHERE claim_id = ?", (claim_id,))
                for ordinal, evidence_id in enumerate(claim_ids):
                    if evidence_id in ids and evidence_id not in missing:
                        database.link_claim_evidence(ClaimEvidenceRow(
                            claim_id=claim_id, evidence_id=evidence_id, ordinal=ordinal,
                        ))
                report["claims_upserted"] += 1
        report["runs_upserted"] += 1
        report["outputs_upserted"] += 1
        report["evidence_links"] += len(ids) - len(missing)

    # Some older deployments kept only llm_analysis_history and no JSONL.  Do
    # not discard those rows: represent them as explicitly unresolved legacy
    # runs, preserving raw_output without inventing evidence links.
    for row in database.list_llm_analysis(limit=limit):
        # Current calls already have a canonical run; do not create a second
        # legacy-analysis run for the same JSONL/history record.
        if database.conn.execute(
            "SELECT 1 FROM llm_runs WHERE json_valid(metadata_json) "
            "AND json_extract(metadata_json, '$.analysis_id') = ? LIMIT 1", (row.id,),
        ).fetchone():
            continue
        report["legacy_records"] += 1
        run_id = f"legacy-analysis:{row.id}"
        try:
            raw_ids = json.loads(row.evidence_json or "[]")
        except Exception:
            raw_ids = []
        ids = [str(value) for value in raw_ids] if isinstance(raw_ids, list) else []
        missing = [value for value in ids if database.get_evidence(value) is None]
        if not ids or missing:
            report["legacy_unresolved"] += 1
        if not apply:
            continue
        parsed = _parse_output(row.raw_output)
        with database.transaction():
            database.upsert_llm_run(LlmRunRow(
                run_id=run_id,
                task_type=row.prompt_id or "legacy_analysis",
                model=row.model,
                prompt_id=row.prompt_id,
                prompt_version=row.prompt_version,
                started_at=row.ts,
                finished_at=row.ts,
                status="legacy_unresolved" if (not ids or missing) else "legacy_linked",
                input_hash="",
                output_hash=hashlib.sha256((row.raw_output or "").encode("utf-8")).hexdigest(),
                error="legacy record has no provable evidence" if (not ids or missing) else "",
                trace_id=row.source_record_id,
                metadata_json=json.dumps({"legacy_analysis_id": row.id}, ensure_ascii=False),
            ))
            report["legacy_runs_upserted"] += 1
            for ordinal, evidence_id in enumerate(ids):
                if evidence_id in missing:
                    continue
                database.link_llm_run_evidence(LlmRunEvidenceRow(
                    run_id=run_id, evidence_id=evidence_id, ordinal=ordinal,
                ))
            database.upsert_llm_output(LlmOutputRow(
                output_id=f"{run_id}:output",
                run_id=run_id,
                output_format="json" if parsed else "text",
                structured_json=json.dumps(parsed, ensure_ascii=False),
                markdown=row.raw_output or row.summary,
                confidence=_confidence(row.confidence),
                limitation="legacy unresolved" if (not ids or missing) else "",
                is_publishable=0,
            ))
    return report


__all__ = ["backfill_llm_provenance"]

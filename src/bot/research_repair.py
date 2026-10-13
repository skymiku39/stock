"""Explicit, reversible quarantine operations for invalid derived research.

The normal audit is read-only.  This module is the separate, opt-in write path
used when a known bad source has been replaced by a new exact source document.
It never deletes raw bytes or relational rows; it only removes the invalid
derived chain from usable answer evidence and records why it was quarantined.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.source_registry import is_source_document_replayable
from bot.stock_db import StockDB
from bot.stock_db import EvidenceLinkRow
from bot.utils import now_tw


def _load_metadata(raw: Any) -> Dict[str, Any]:
    try:
        metadata = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        metadata = {}
    return metadata if isinstance(metadata, dict) else {}


def _collect_derived_chain_ids(
    database: StockDB,
    source_id: str,
) -> Dict[str, List[str]]:
    """Resolve exact derived IDs for one known source document. Never guesses."""
    research_ids = [
        str(row[0]) for row in database.conn.execute(
            "SELECT document_id FROM research_documents "
            "WHERE source_document_id = ? ORDER BY document_id",
            (source_id,),
        ).fetchall()
    ]
    evidence_ids = [
        str(row[0]) for row in database.conn.execute(
            "SELECT evidence_id FROM evidence_items "
            "WHERE source_document_id = ? ORDER BY evidence_id",
            (source_id,),
        ).fetchall()
    ]
    claim_ids = [
        str(row[0]) for row in database.conn.execute(
            "SELECT DISTINCT c.claim_id FROM claims c "
            "JOIN claim_evidence ce ON ce.claim_id = c.claim_id "
            "WHERE ce.evidence_id IN (" + ",".join("?" for _ in evidence_ids) + ") "
            "ORDER BY c.claim_id",
            evidence_ids,
        ).fetchall()
    ] if evidence_ids else []
    run_ids = [
        str(row[0]) for row in database.conn.execute(
            "SELECT DISTINCT r.run_id FROM llm_runs r "
            "JOIN llm_run_evidence re ON re.run_id = r.run_id "
            "WHERE re.evidence_id IN (" + ",".join("?" for _ in evidence_ids) + ") "
            "ORDER BY r.run_id",
            evidence_ids,
        ).fetchall()
    ] if evidence_ids else []
    return {
        "research_document_ids": research_ids,
        "evidence_ids": evidence_ids,
        "claim_ids": claim_ids,
        "llm_run_ids": run_ids,
    }


def _chain_counts(ids: Dict[str, List[str]]) -> Dict[str, int]:
    return {
        "research_documents": len(ids["research_document_ids"]),
        "evidence_items": len(ids["evidence_ids"]),
        "claims": len(ids["claim_ids"]),
        "llm_runs": len(ids["llm_run_ids"]),
    }


def plan_quarantine_research_chain(
    *,
    root: Path,
    source_document_id: str,
    replacement_source_document_id: str = "",
    reason: str = "",
    db: Optional[StockDB] = None,
) -> Dict[str, Any]:
    """Read-only preview of the exact derived chain for one source document."""
    source_id = str(source_document_id or "").strip()
    replacement_id = str(replacement_source_document_id or "").strip()
    why = str(reason or "").strip()
    if not source_id:
        raise ValueError("source_document_id is required")

    owns_db = db is None
    database = db or StockDB.open(root=Path(root))
    try:
        source = database.get_source_document(source_id)
        if source is None:
            raise ValueError(f"unknown source_document_id: {source_id}")
        if replacement_id and database.get_source_document(replacement_id) is None:
            raise ValueError(f"unknown replacement source_document_id: {replacement_id}")
        ids = _collect_derived_chain_ids(database, source_id)
        return {
            "status": "dry_run",
            "dry_run": True,
            "source_document_id": source_id,
            "replacement_source_document_id": replacement_id,
            "reason": why,
            **_chain_counts(ids),
            **ids,
        }
    finally:
        if owns_db:
            database.close()


def list_quarantined_research_chains(
    *,
    root: Path,
    db: Optional[StockDB] = None,
) -> Dict[str, Any]:
    """List source documents already marked with an explicit quarantine record."""
    owns_db = db is None
    database = db or StockDB.open(root=Path(root))
    try:
        chains: List[Dict[str, Any]] = []
        for row in database.conn.execute(
            "SELECT document_id, source, quality_status, metadata_json "
            "FROM source_documents ORDER BY document_id"
        ).fetchall():
            metadata = _load_metadata(row[3])
            quarantine = metadata.get("quarantine")
            if not isinstance(quarantine, dict):
                continue
            source_id = str(row[0])
            ids = _collect_derived_chain_ids(database, source_id)
            chains.append({
                "source_document_id": source_id,
                "source": str(row[1] or ""),
                "quality_status": str(row[2] or ""),
                "reason": str(quarantine.get("reason") or ""),
                "quarantined_at": str(quarantine.get("quarantined_at") or ""),
                "replacement_source_document_id": str(
                    quarantine.get("replacement_source_document_id") or ""
                ),
                **_chain_counts(ids),
                **ids,
            })
        return {
            "status": "listed",
            "dry_run": True,
            "quarantined_chains": chains,
            "count": len(chains),
        }
    finally:
        if owns_db:
            database.close()


def quarantine_research_chain(
    *,
    root: Path,
    source_document_id: str,
    replacement_source_document_id: str = "",
    reason: str,
    db: Optional[StockDB] = None,
) -> Dict[str, Any]:
    """Quarantine all derived rows for one exact source document.

    ``replacement_source_document_id`` is optional because quarantine can be
    used before a replacement is available.  When supplied, it must already
    exist; the function never guesses or creates a replacement identity.
    """
    source_id = str(source_document_id or "").strip()
    replacement_id = str(replacement_source_document_id or "").strip()
    why = str(reason or "").strip()
    if not source_id:
        raise ValueError("source_document_id is required")
    if not why:
        raise ValueError("reason is required")

    owns_db = db is None
    database = db or StockDB.open(root=Path(root))
    try:
        source = database.get_source_document(source_id)
        if source is None:
            raise ValueError(f"unknown source_document_id: {source_id}")
        if replacement_id and database.get_source_document(replacement_id) is None:
            raise ValueError(f"unknown replacement source_document_id: {replacement_id}")

        ids = _collect_derived_chain_ids(database, source_id)
        research_ids = ids["research_document_ids"]
        evidence_ids = ids["evidence_ids"]
        claim_ids = ids["claim_ids"]
        run_ids = ids["llm_run_ids"]

        timestamp = now_tw().isoformat(timespec="seconds")
        quarantine = {
            "reason": why,
            "quarantined_at": timestamp,
            "replacement_source_document_id": replacement_id,
            "policy": "raw_preserved_derived_chain_not_usable",
        }

        def merged_metadata(raw: Any) -> str:
            metadata = _load_metadata(raw)
            metadata["quarantine"] = quarantine
            return json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)

        with database.transaction() as conn:
            conn.execute(
                "UPDATE source_documents SET quality_status = 'invalid', "
                "metadata_json = ?, updated_at = ? WHERE document_id = ?",
                (merged_metadata(source.metadata_json), timestamp, source_id),
            )
            for research_id in research_ids:
                row = conn.execute(
                    "SELECT metadata_json FROM research_documents WHERE document_id = ?",
                    (research_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE research_documents SET status = 'unresolved', metadata_json = ?, "
                    "updated_at = ? WHERE document_id = ?",
                    (merged_metadata(row[0] if row else "{}"), timestamp, research_id),
                )
            for evidence_id in evidence_ids:
                row = conn.execute(
                    "SELECT metadata_json FROM evidence_items WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE evidence_items SET quality_status = 'invalid', metadata_json = ?, "
                    "updated_at = ? WHERE evidence_id = ?",
                    (merged_metadata(row[0] if row else "{}"), timestamp, evidence_id),
                )
            if claim_ids:
                placeholders = ",".join("?" for _ in claim_ids)
                conn.execute(
                    "UPDATE claims SET status = 'unverified', confidence = 0.0, "
                    "uncertainty = ?, updated_at = ? WHERE claim_id IN (" + placeholders + ")",
                    [
                        "來源文件格式錯誤，原始衍生鏈已隔離；不可作為回答證據。",
                        timestamp,
                        *claim_ids,
                    ],
                )
            if run_ids:
                for run_id in run_ids:
                    row = conn.execute(
                        "SELECT metadata_json FROM llm_runs WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                    conn.execute(
                        "UPDATE llm_runs SET status = 'source_quarantined', "
                        "error = ?, metadata_json = ? WHERE run_id = ?",
                        (why, merged_metadata(row[0] if row else "{}"), run_id),
                    )

        return {
            "status": "quarantined",
            "dry_run": False,
            "source_document_id": source_id,
            "replacement_source_document_id": replacement_id,
            "reason": why,
            **_chain_counts(ids),
            **ids,
        }
    finally:
        if owns_db:
            database.close()


def repair_evidence_source_links(
    *,
    root: Path,
    db: Optional[StockDB] = None,
    apply: bool = False,
    limit: int = 100_000,
) -> Dict[str, int | bool]:
    """補回可由既有 exact metadata 證明的 evidence source links。

    歷史版本曾用單一 ``evidence-source:<evidence_id>`` link ID，導致同一
    evidence ID 在不同 raw 版本重用時，後一次來源覆蓋前一次連結。這個
    修復只從既有 ``llm_runs.metadata_json``、每日報告 packet，以及
    ``evidence_items.source_document_id`` 取得明確 source identity；不依
    URL、標題、文字或相似度猜測。預設 dry-run，``apply=True`` 只新增
    source-scoped links，且可安全重跑。
    """
    project_root = Path(root).resolve()
    database = db or StockDB.open(root=project_root)
    owns_db = db is None
    max_items = max(0, int(limit))
    try:
        candidates: set[tuple[str, str]] = set()

        # Current evidence rows are authoritative for their explicitly stored
        # primary source and make the repair useful even without run metadata.
        for row in database.conn.execute(
            "SELECT evidence_id, source_document_id FROM evidence_items "
            "WHERE source_document_id <> '' LIMIT ?",
            (max_items or 100_000,),
        ).fetchall():
            candidates.add((str(row[0]), str(row[1])))

        run_rows = database.conn.execute(
            "SELECT r.metadata_json, re.evidence_id "
            "FROM llm_runs r JOIN llm_run_evidence re ON re.run_id = r.run_id "
            "ORDER BY r.run_id, re.ordinal LIMIT ?",
            (max_items or 100_000,),
        ).fetchall()
        for row in run_rows:
            try:
                metadata = json.loads(str(row[0] or "{}"))
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                continue
            source_ids = []
            primary = str(metadata.get("source_document_id") or "").strip()
            if primary:
                source_ids.append(primary)
            raw_ids = metadata.get("source_document_ids")
            if isinstance(raw_ids, list):
                source_ids.extend(str(value).strip() for value in raw_ids if str(value).strip())
            for source_id in dict.fromkeys(source_ids):
                candidates.add((str(row[1]), source_id))

        report_rows = database.conn.execute(
            "SELECT evidence_json FROM llm_daily_reports LIMIT ?",
            (max_items or 100_000,),
        ).fetchall()
        for row in report_rows:
            try:
                packets = json.loads(str(row[0] or "[]"))
            except (TypeError, ValueError):
                packets = []
            if not isinstance(packets, list):
                continue
            for packet in packets:
                if not isinstance(packet, dict):
                    continue
                evidence_id = str(packet.get("id") or packet.get("evidence_id") or "").strip()
                source_id = str(packet.get("source_document_id") or "").strip()
                if evidence_id and source_id:
                    candidates.add((evidence_id, source_id))

        existing = {
            (str(row[0]), str(row[1]))
            for row in database.conn.execute(
                "SELECT evidence_id, source_document_id FROM evidence_links "
                "WHERE source_document_id <> ''"
            ).fetchall()
        }
        report: Dict[str, int | bool] = {
            "dry_run": not apply,
            "candidate_pairs": len(candidates),
            "already_linked": 0,
            "links_added": 0,
            "missing_evidence": 0,
            "unknown_source_document": 0,
            "not_replayable": 0,
        }
        for evidence_id, source_id in sorted(candidates):
            if (evidence_id, source_id) in existing:
                report["already_linked"] += 1
                continue
            evidence = database.get_evidence(evidence_id)
            if evidence is None:
                report["missing_evidence"] += 1
                continue
            source = database.get_source_document(source_id)
            if source is None:
                report["unknown_source_document"] += 1
                continue
            if not is_source_document_replayable(project_root, source):
                report["not_replayable"] += 1
                continue
            if apply:
                source_digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16]
                database.upsert_evidence_link(EvidenceLinkRow(
                    link_id=f"evidence-source:{evidence_id}:{source_digest}",
                    evidence_id=evidence_id,
                    source_document_id=source_id,
                    subject_type="source_document",
                    subject_id=source_id,
                    quote_or_summary=evidence.excerpt,
                    as_of=evidence.as_of or evidence.observed_at,
                ))
                existing.add((evidence_id, source_id))
            report["links_added"] += 1
        return report
    finally:
        if owns_db:
            database.close()


__all__ = [
    "list_quarantined_research_chains",
    "plan_quarantine_research_chain",
    "quarantine_research_chain",
    "repair_evidence_source_links",
]

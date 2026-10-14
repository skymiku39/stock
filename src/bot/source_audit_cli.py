"""``stock-data-audit`` -- read-only provenance gap report.

This command does not fetch data and does not rewrite business tables.  It
compares the current SQLite/index state with files under ``data/`` and
``log/`` so a migration can be planned from evidence instead of guesses.

Examples
========
    uv run stock-data-audit
    uv run stock-data-audit --root C:\\skymiku\\stock --json
    uv run stock-data-audit --fail-on-gap
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from bot.stock_db import SourceDocumentRow, default_db_path
from bot.canonical_audit import audit_canonical_database
from bot.research_audit import audit_research_database
from bot.source_registry import is_source_document_replayable
from bot.llm_provenance import evidence_ids as normalize_evidence_ids


_TRACKED_TABLES = (
    "source_runs",
    "source_documents",
    "ingestion_runs",
    "source_health",
    "evidence_items",
    "memory_items",
    "llm_analysis_history",
    "llm_daily_reports",
    "research_documents",
    "research_chunks",
    "evidence_links",
    "llm_runs",
    "llm_run_evidence",
    "llm_outputs",
    "claims",
    "claim_evidence",
    "memory_events",
    "memory_evidence",
    "memory_proposals",
)


def _file_summary(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"files": 0, "bytes": 0}
    files = [item for item in path.rglob("*") if item.is_file()]
    return {
        "files": len(files),
        "bytes": sum(item.stat().st_size for item in files),
    }


def _relative_files(root: Path, directory: Path) -> Iterable[str]:
    if not directory.exists():
        return ()
    return (
        str(item.relative_to(root)).replace("\\", "/")
        for item in directory.rglob("*")
        if item.is_file()
    )


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _llm_provenance_report(conn: sqlite3.Connection, tables: set[str]) -> Dict[str, Any]:
    """Read execution and linkage separately, checking receipts against SQL."""
    counts = dict.fromkeys((
        "linked", "partial", "successful_without_evidence", "failed",
        "not_required", "legacy_unresolved", "unassessed",
    ), 0)
    result: Dict[str, Any] = {
        "counts": counts, "by_task": [], "without_evidence": 0,
        "legacy_without_evidence": 0, "planner_analysis_ids": set(),
    }
    if not {"llm_runs", "llm_run_evidence", "evidence_items"} <= tables:
        return result
    linked: Dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT x.run_id, x.evidence_id FROM llm_run_evidence x "
        "JOIN evidence_items e ON e.evidence_id=x.evidence_id"
    ):
        linked.setdefault(str(row["run_id"]), set()).add(str(row["evidence_id"]))
    groups: Dict[tuple[str, str, str], int] = {}
    analysis_classes: Dict[str, set[str]] = {}
    for row in conn.execute("SELECT run_id, task_type, prompt_id, status, metadata_json FROM llm_runs"):
        try:
            md = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            md = {}
        md = md if isinstance(md, dict) else {}
        receipt = md.get("input_evidence")
        receipt = receipt if isinstance(receipt, dict) and receipt.get("version") == 1 else {}
        if receipt.get("state") not in {"linked", "partial", "missing", "not_required", "unassessed"}:
            receipt = {}
        expected = set(normalize_evidence_ids(md.get("evidence_ids")))
        actual = linked.get(str(row["run_id"]), set())
        status = str(row["status"])
        if status == "legacy_unresolved":
            category = "legacy_unresolved"
        elif status == "failed":
            category = "failed"
        elif (
            receipt.get("state") == "not_required"
            and row["task_type"] == row["prompt_id"] == "query_plan"
            and not expected and not actual
        ):
            category = "not_required"
        elif not receipt or receipt.get("state") == "unassessed":
            category = "unassessed"
        elif expected and actual == expected:
            category = "linked"
        elif actual:
            category = "partial"
        else:
            category = "successful_without_evidence"
        counts[category] += 1
        key = (str(row["task_type"]), status, category)
        groups[key] = groups.get(key, 0) + 1
        analysis_id = str(md.get("analysis_id") or "")
        if analysis_id:
            analysis_classes.setdefault(analysis_id, set()).add(category)
        if not actual:
            if category == "legacy_unresolved":
                result["legacy_without_evidence"] += 1
            elif category not in {"failed", "not_required"}:
                result["without_evidence"] += 1
    result["planner_analysis_ids"] = {
        key for key, categories in analysis_classes.items() if categories == {"not_required"}
    }
    result["by_task"] = [
        {"task_type": task, "execution_status": status, "evidence_category": category, "count": count}
        for (task, status, category), count in sorted(groups.items())
    ]
    return result


def _db_report(db_path: Path, *, root: Optional[Path] = None) -> Dict[str, Any]:
    if not db_path.exists():
        return {
            "exists": False,
            "path": str(db_path),
            "tables": {},
            "schema_version": 0,
            "source_health": [],
            "canonical": {
                "exists": False, "path": str(db_path), "tables": {},
                "rows_checked": 0, "invalid_tables": 0,
                "partial_tables": 0, "empty_tables": 0,
            },
            "research": audit_research_database(db_path, root=root),
        }

    conn = _open_readonly(db_path)
    try:
        table_names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        counts: Dict[str, int] = {}
        for table in _TRACKED_TABLES:
            if table in table_names:
                counts[table] = int(
                    conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
            else:
                counts[table] = 0
        migrations = []
        if "schema_migrations" in table_names:
            migrations = [
                dict(row)
                for row in conn.execute(
                    "SELECT version, name, checksum, applied_at "
                    "FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
        health = []
        if "source_health" in table_names:
            health = [
                dict(row)
                for row in conn.execute(
                    "SELECT source, last_status, checked_at, consecutive_failures, "
                    "last_error FROM source_health ORDER BY source"
                ).fetchall()
            ]
        running_ingestion_runs = []
        if "ingestion_runs" in table_names:
            running_ingestion_runs = [
                dict(row)
                for row in conn.execute(
                    "SELECT run_id, source, started_at FROM ingestion_runs "
                    "WHERE status = 'running' ORDER BY started_at"
                ).fetchall()
            ]
        integrity: Dict[str, Any] = {
            "sqlite": {
                "status": "unknown",
                "details": [],
            },
            "orphan_counts": {},
            "fts": {},
        }
        try:
            sqlite_check = [
                str(row[0])
                for row in conn.execute("PRAGMA integrity_check").fetchall()
            ]
            integrity["sqlite"] = {
                "status": "ok" if sqlite_check == ["ok"] else "failed",
                "details": sqlite_check,
            }
        except sqlite3.DatabaseError as exc:
            integrity["sqlite"] = {
                "status": "failed",
                "details": [str(exc)],
            }
        if "sync_conflicts" in table_names:
            integrity["sync_conflicts"] = {
                "total": int(conn.execute(
                    "SELECT COUNT(*) FROM sync_conflicts"
                ).fetchone()[0]),
                "open": int(conn.execute(
                    "SELECT COUNT(*) FROM sync_conflicts WHERE status='open'"
                ).fetchone()[0]),
            }

        def _record_orphan_count(name: str, sql: str) -> None:
            try:
                integrity["orphan_counts"][name] = int(conn.execute(sql).fetchone()[0])
            except sqlite3.DatabaseError:
                # 舊版資料庫可能還沒有某張研究／記憶表；這是相容狀態，
                # 不應讓 read-only audit 失敗。
                return

        if {"memory_evidence", "memory_items", "evidence_items"} <= table_names:
            _record_orphan_count(
                "memory_evidence",
                "SELECT COUNT(*) FROM memory_evidence me "
                "LEFT JOIN memory_items m ON m.memory_id=me.memory_id "
                "LEFT JOIN evidence_items e ON e.evidence_id=me.evidence_id "
                "WHERE m.memory_id IS NULL OR e.evidence_id IS NULL",
            )
        if {"claim_evidence", "claims", "evidence_items"} <= table_names:
            _record_orphan_count(
                "claim_evidence",
                "SELECT COUNT(*) FROM claim_evidence ce "
                "LEFT JOIN claims c ON c.claim_id=ce.claim_id "
                "LEFT JOIN evidence_items e ON e.evidence_id=ce.evidence_id "
                "WHERE c.claim_id IS NULL OR e.evidence_id IS NULL",
            )
            _record_orphan_count(
                "claims_without_evidence",
                "SELECT COUNT(*) FROM claims c "
                "WHERE NOT EXISTS (SELECT 1 FROM claim_evidence ce "
                "WHERE ce.claim_id=c.claim_id)",
            )
        if {"llm_run_evidence", "llm_runs", "evidence_items"} <= table_names:
            _record_orphan_count(
                "llm_run_evidence",
                "SELECT COUNT(*) FROM llm_run_evidence le "
                "LEFT JOIN llm_runs r ON r.run_id=le.run_id "
                "LEFT JOIN evidence_items e ON e.evidence_id=le.evidence_id "
                "WHERE r.run_id IS NULL OR e.evidence_id IS NULL",
            )
        if {"llm_outputs", "llm_runs"} <= table_names:
            _record_orphan_count(
                "llm_outputs",
                "SELECT COUNT(*) FROM llm_outputs o "
                "LEFT JOIN llm_runs r ON r.run_id=o.run_id "
                "WHERE r.run_id IS NULL",
            )
        if {"research_documents", "source_documents"} <= table_names:
            _record_orphan_count(
                "research_document_sources",
                "SELECT COUNT(*) FROM research_documents d "
                "LEFT JOIN source_documents s ON s.document_id=d.source_document_id "
                "WHERE d.source_document_id<>'' AND s.document_id IS NULL",
            )
        if {"research_chunks", "research_documents"} <= table_names:
            _record_orphan_count(
                "research_chunks",
                "SELECT COUNT(*) FROM research_chunks c "
                "LEFT JOIN research_documents d ON d.document_id=c.document_id "
                "WHERE d.document_id IS NULL",
            )
        if {"evidence_items", "source_documents"} <= table_names:
            _record_orphan_count(
                "evidence_sources",
                "SELECT COUNT(*) FROM evidence_items e "
                "LEFT JOIN source_documents s ON s.document_id=e.source_document_id "
                "WHERE e.source_document_id<>'' AND s.document_id IS NULL",
            )

        # Canonical facts use ``source_record_id`` for backward-compatible
        # provenance.  Only IDs with our exact manifest prefix are checked;
        # legacy source labels such as ``twse`` remain valid non-exact labels
        # and must not be reported as fabricated document IDs.
        for table in (
            "stock_info", "price_history", "monthly_revenue", "valuation_daily",
            "dividend_history", "quarterly_report", "intraday_bars",
            "llm_analysis_history", "llm_daily_reports", "memory_items",
        ):
            if table not in table_names:
                continue
            columns = {
                str(row[1]) for row in conn.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            }
            if "source_record_id" not in columns:
                continue
            _record_orphan_count(
                f"{table}_source_records",
                f'SELECT COUNT(*) FROM "{table}" t '
                "LEFT JOIN source_documents s ON s.document_id=t.source_record_id "
                "WHERE t.source_record_id LIKE 'source-doc:%' AND s.document_id IS NULL",
            )

        for base, index_name in (
            ("memory_items", "memory_items_fts"),
            ("research_chunks", "research_chunks_fts"),
        ):
            if base not in table_names:
                continue
            base_count = int(conn.execute(f'SELECT COUNT(*) FROM "{base}"').fetchone()[0])
            index_exists = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (index_name,)
            ).fetchone())
            index_count = None
            if index_exists:
                try:
                    index_count = int(conn.execute(f'SELECT COUNT(*) FROM "{index_name}"').fetchone()[0])
                except sqlite3.DatabaseError:
                    index_count = None
            integrity["fts"][base] = {
                "index": index_name,
                "available": index_count is not None,
                "base_count": base_count,
                "index_count": index_count,
                "consistent": index_count is None or index_count == base_count,
            }
        llm_without_evidence = 0
        llm_legacy_unresolved = 0
        llm_with_unresolved_evidence = 0
        llm_failed = 0
        llm_not_required = 0
        llm_provenance = _llm_provenance_report(conn, table_names)
        if "llm_analysis_history" in table_names:
            registered_evidence_ids = set()
            legacy_run_ids = set()
            if "evidence_items" in table_names:
                registered_evidence_ids = {
                    str(row[0]) for row in conn.execute(
                        "SELECT evidence_id FROM evidence_items"
                    ).fetchall()
                }
            if "llm_runs" in table_names:
                legacy_run_ids = {
                    str(row[0]) for row in conn.execute(
                        "SELECT run_id FROM llm_runs WHERE status = 'legacy_unresolved'"
                    ).fetchall()
                }
            for row in conn.execute(
                "SELECT id, evidence_json, success FROM llm_analysis_history"
            ).fetchall():
                if not row["success"]:
                    llm_failed += 1
                    continue
                if str(row["id"]) in llm_provenance["planner_analysis_ids"]:
                    llm_not_required += 1
                    continue
                raw_evidence = str(row["evidence_json"] or "").strip()
                if raw_evidence in {"", "[]", "{}"}:
                    if f"legacy-analysis:{row['id']}" in legacy_run_ids:
                        llm_legacy_unresolved += 1
                    else:
                        llm_without_evidence += 1
                    continue
                try:
                    ids = json.loads(raw_evidence)
                except (TypeError, ValueError):
                    ids = []
                if (
                    not isinstance(ids, list)
                    or not ids
                    or any(str(evidence_id) not in registered_evidence_ids for evidence_id in ids)
                ):
                    if f"legacy-analysis:{row['id']}" in legacy_run_ids:
                        llm_legacy_unresolved += 1
                    else:
                        llm_with_unresolved_evidence += 1
        documents = []
        document_manifest = []
        source_runs_without_documents = 0
        unresolved_source_runs = []
        if "source_documents" in table_names:
            document_columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(source_documents)"
                ).fetchall()
            }
            hash_expression = "content_hash" if "content_hash" in document_columns else "''"
            documents = [
                str(row[0]).replace("\\", "/")
                for row in conn.execute(
                    "SELECT content_path FROM source_documents "
                    "WHERE content_path <> ''"
                ).fetchall()
            ]
            document_manifest = [
                {
                    "content_path": str(row[0]).replace("\\", "/"),
                    "content_hash": str(row[1] or ""),
                }
                for row in conn.execute(
                    f"SELECT content_path, {hash_expression} AS content_hash "
                    "FROM source_documents WHERE content_path <> ''"
                ).fetchall()
            ]
        if "source_runs" in table_names and "source_documents" in table_names:
            document_columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(source_documents)"
                ).fetchall()
            }
            if "ingestion_run_id" in document_columns:
                direct_run_ids = {
                    str(row[0]) for row in conn.execute(
                        "SELECT DISTINCT ingestion_run_id FROM source_documents "
                        "WHERE ingestion_run_id <> ''"
                    ).fetchall()
                }
                document_ids = {
                    str(row[0]) for row in conn.execute(
                        "SELECT document_id FROM source_documents"
                    ).fetchall()
                }
                for source_run in conn.execute(
                    "SELECT run_id, source, source_url, status, started_at, completed_at, "
                    "source_date, row_count, raw_hash, error, details_json "
                    "FROM source_runs ORDER BY started_at, run_id"
                ).fetchall():
                    run_id = str(source_run["run_id"] or "")
                    if run_id in direct_run_ids:
                        continue
                    try:
                        details = json.loads(str(source_run["details_json"] or "{}"))
                    except (TypeError, ValueError):
                        details = {}
                    linked_ids = details.get("document_ids") if isinstance(details, dict) else []
                    linked_ids = [str(document_id) for document_id in linked_ids] if isinstance(
                        linked_ids, list
                    ) else []
                    resolved_linked_ids = [
                        document_id for document_id in linked_ids if document_id in document_ids
                    ]
                    if resolved_linked_ids:
                        continue
                    source_runs_without_documents += 1
                    if linked_ids:
                        reason_code = "source_document_reference_unresolved"
                        remediation = (
                            "確認 details_json 內的 document_ids 是否來自同一個資料庫；"
                            "若原始文件不存在，重新以 raw-first adapter 抓取。"
                        )
                    else:
                        reason_code = "source_document_link_missing"
                        remediation = (
                            "以 raw-first adapter 重新抓取並建立 source_documents；"
                            "不得依 URL、標題或相似度猜補 document_id。"
                        )
                    unresolved_source_runs.append({
                        "run_id": run_id,
                        "source": str(source_run["source"] or ""),
                        "status": str(source_run["status"] or ""),
                        "started_at": str(source_run["started_at"] or ""),
                        "completed_at": str(source_run["completed_at"] or ""),
                        "error": str(source_run["error"] or ""),
                        "source_date": str(source_run["source_date"] or ""),
                        "row_count": int(source_run["row_count"] or 0),
                        "reason_code": reason_code,
                        "remediation": remediation,
                        "source_url_present": bool(str(source_run["source_url"] or "").strip()),
                        "raw_hash_present": bool(str(source_run["raw_hash"] or "").strip()),
                        "raw_hash_exact_document_ids": [
                            str(row[0]) for row in conn.execute(
                                "SELECT document_id FROM source_documents "
                                "WHERE content_hash = ? ORDER BY document_id",
                                (str(source_run["raw_hash"] or "").strip(),),
                            ).fetchall()
                            if str(source_run["raw_hash"] or "").strip()
                        ],
                        "linked_document_ids": linked_ids,
                        "details_keys": sorted(
                            str(key) for key in details.keys()
                        ) if isinstance(details, dict) else [],
                    })
        llm_runs_without_evidence = llm_provenance["without_evidence"]
        llm_runs_legacy_unresolved = llm_provenance["legacy_without_evidence"]
        conflicted_claims = 0
        if "claims" in table_names:
            conflicted_claims = int(conn.execute(
                "SELECT COUNT(*) FROM claims WHERE status='conflicted'"
            ).fetchone()[0])

        # Daily reports are aggregate artifacts and therefore deliberately do
        # not use a single ``source_record_id`` when they contain evidence
        # from several sources.  Audit their packet instead of treating a
        # blank row-level source ID as a provenance failure.  This catches old
        # report replay/sync paths that wrote legacy evidence directly without
        # passing the exact raw-document gate.
        daily_reports_with_unresolved_evidence = 0
        daily_report_unresolved_evidence_items = 0
        daily_reports_without_evidence = 0
        daily_report_findings: List[Dict[str, Any]] = []
        if {"llm_daily_reports", "evidence_items", "source_documents"} <= table_names:
            evidence_rows = conn.execute(
                "SELECT evidence_id, source_document_id FROM evidence_items"
            ).fetchall()
            evidence_sources = {
                str(row["evidence_id"]): str(row["source_document_id"] or "")
                for row in evidence_rows
            }
            evidence_source_links = {
                (
                    str(row["evidence_id"]),
                    str(row["source_document_id"] or ""),
                )
                for row in conn.execute(
                    "SELECT evidence_id, source_document_id FROM evidence_links"
                ).fetchall()
                if str(row["source_document_id"] or "").strip()
            } if "evidence_links" in table_names else set()
            source_document_rows = {
                str(row["document_id"]): SourceDocumentRow(**dict(row))
                for row in conn.execute("SELECT * FROM source_documents").fetchall()
            }
            replay_root = Path(root or db_path.parent.parent).resolve()
            for report in conn.execute(
                "SELECT report_type, report_date, mode, quality_status, evidence_json "
                "FROM llm_daily_reports ORDER BY report_date, report_type, mode"
            ).fetchall():
                raw_evidence = str(report["evidence_json"] or "").strip()
                parsed_evidence = True
                try:
                    packets = json.loads(raw_evidence) if raw_evidence else []
                except (TypeError, ValueError):
                    packets = None
                    parsed_evidence = False
                reasons: List[Dict[str, Any]] = []
                if not isinstance(packets, list):
                    reasons.append({
                        "index": None,
                        "evidence_id": "",
                        "reasons": ["evidence_json_not_list"],
                    })
                    packets = []
                if (
                    parsed_evidence
                    and not packets
                    and str(report["quality_status"] or "").lower() == "ok"
                ):
                    daily_reports_without_evidence += 1
                for index, packet in enumerate(packets):
                    packet_reasons: List[str] = []
                    if not isinstance(packet, dict):
                        packet_reasons.append("packet_not_object")
                        evidence_id = ""
                        source_document_id = ""
                    else:
                        evidence_id = str(
                            packet.get("id") or packet.get("evidence_id") or ""
                        ).strip()
                        source_document_id = str(
                            packet.get("source_document_id") or ""
                        ).strip()
                        if not evidence_id:
                            packet_reasons.append("evidence_id_missing")
                        elif evidence_id not in evidence_sources:
                            packet_reasons.append("evidence_item_missing")
                            if not source_document_id:
                                packet_reasons.append("source_document_id_missing")
                        else:
                            registered_source_id = evidence_sources[evidence_id]
                            if not source_document_id:
                                packet_reasons.append("source_document_id_missing")
                            elif (
                                registered_source_id != source_document_id
                                and (evidence_id, source_document_id)
                                not in evidence_source_links
                            ):
                                packet_reasons.append("evidence_source_mismatch")
                        if str(packet.get("provenance_status") or "").lower() not in {
                            "exact", "composite_exact"
                        }:
                            packet_reasons.append("provenance_not_exact")
                    source_document = source_document_rows.get(source_document_id)
                    if source_document_id and source_document is None:
                        packet_reasons.append("source_document_missing")
                    elif source_document_id and source_document is not None:
                        if not is_source_document_replayable(replay_root, source_document):
                            packet_reasons.append("raw_archive_not_replayable")
                    if packet_reasons:
                        reasons.append({
                            "index": index,
                            "evidence_id": evidence_id,
                            "reasons": sorted(set(packet_reasons)),
                        })
                if reasons:
                    daily_reports_with_unresolved_evidence += 1
                    daily_report_unresolved_evidence_items += len(reasons)
                    if len(daily_report_findings) < 1000:
                        daily_report_findings.append({
                            "report_type": str(report["report_type"] or ""),
                            "report_date": str(report["report_date"] or ""),
                            "mode": str(report["mode"] or ""),
                            "quality_status": str(report["quality_status"] or ""),
                            "unresolved_items": reasons,
                        })

        # Research ingest runs also carry an explicit source document in their
        # metadata.  Verify the complete run -> evidence -> source link chain
        # independently from the daily-report packet check so an old link-ID
        # overwrite cannot hide a missing historical source endpoint.
        research_missing_source_links = 0
        research_invalid_source_links = 0
        research_source_link_findings: List[Dict[str, Any]] = []
        if {
            "llm_runs", "llm_run_evidence", "evidence_items",
            "evidence_links", "source_documents",
        } <= table_names:
            evidence_ids = {
                str(row["evidence_id"])
                for row in conn.execute("SELECT evidence_id FROM evidence_items").fetchall()
            }
            linked_source_pairs = {
                (str(row["evidence_id"]), str(row["source_document_id"] or ""))
                for row in conn.execute(
                    "SELECT evidence_id, source_document_id FROM evidence_links"
                ).fetchall()
                if str(row["source_document_id"] or "").strip()
            }
            source_rows = {
                str(row["document_id"]): SourceDocumentRow(**dict(row))
                for row in conn.execute("SELECT * FROM source_documents").fetchall()
            }
            run_source_pairs: Dict[tuple[str, str], List[str]] = {}
            for row in conn.execute(
                "SELECT r.run_id, r.metadata_json, re.evidence_id "
                "FROM llm_runs r JOIN llm_run_evidence re ON re.run_id = r.run_id"
            ).fetchall():
                try:
                    metadata = json.loads(str(row["metadata_json"] or "{}"))
                except (TypeError, ValueError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    continue
                source_ids: List[str] = []
                source_id = str(metadata.get("source_document_id") or "").strip()
                if source_id:
                    source_ids.append(source_id)
                raw_source_ids = metadata.get("source_document_ids")
                if isinstance(raw_source_ids, list):
                    source_ids.extend(
                        str(value).strip()
                        for value in raw_source_ids
                        if str(value).strip()
                    )
                for source_id in dict.fromkeys(source_ids):
                    pair = (str(row["evidence_id"]), source_id)
                    run_source_pairs.setdefault(pair, []).append(str(row["run_id"]))
            # A direct evidence write must also have a source link, even if it
            # did not originate from an llm_run.
            for row in conn.execute(
                "SELECT evidence_id, source_document_id FROM evidence_items "
                "WHERE source_document_id <> ''"
            ).fetchall():
                run_source_pairs.setdefault(
                    (str(row["evidence_id"]), str(row["source_document_id"])),
                    [],
                )
            replay_root = Path(root or db_path.parent.parent).resolve()
            for (evidence_id, source_id), run_ids in sorted(run_source_pairs.items()):
                if (evidence_id, source_id) in linked_source_pairs:
                    continue
                reasons: List[str] = []
                if evidence_id not in evidence_ids:
                    reasons.append("evidence_item_missing")
                source = source_rows.get(source_id)
                if source is None:
                    reasons.append("source_document_missing")
                elif not is_source_document_replayable(replay_root, source):
                    reasons.append("raw_archive_not_replayable")
                if not reasons:
                    research_missing_source_links += 1
                    reasons.append("evidence_source_link_missing")
                else:
                    research_invalid_source_links += 1
                if len(research_source_link_findings) < 1000:
                    research_source_link_findings.append({
                        "evidence_id": evidence_id,
                        "source_document_id": source_id,
                        "run_ids": run_ids[:10],
                        "reasons": sorted(set(reasons)),
                    })
        return {
            "exists": True,
            "path": str(db_path),
            "bytes": db_path.stat().st_size,
            "tables": counts,
            # Keep the historical report field at the v1 compatibility
            # baseline; ``latest_schema_version`` exposes the real research
            # migration without breaking existing dashboards.
            "schema_version": min(max(
                [int(row["version"]) for row in migrations] or [0]
            ), 1),
            "latest_schema_version": max(
                [int(row["version"]) for row in migrations] or [0]
            ),
            "migrations": migrations,
            "source_health": health,
            "running_ingestion_runs": running_ingestion_runs,
            "integrity": integrity,
            "registered_content_paths": documents,
            "document_manifest": document_manifest,
            "source_runs_without_documents": source_runs_without_documents,
            "unresolved_source_runs": unresolved_source_runs,
            "llm_analysis_without_evidence": llm_without_evidence,
            "llm_analysis_legacy_unresolved": llm_legacy_unresolved,
            "llm_analysis_with_unresolved_evidence": llm_with_unresolved_evidence,
            "llm_analysis_failed": llm_failed,
            "llm_analysis_evidence_not_required": llm_not_required,
            "llm_runs_without_evidence": llm_runs_without_evidence,
            "llm_runs_legacy_unresolved": llm_runs_legacy_unresolved,
            "llm_run_provenance": {
                "counts": llm_provenance["counts"], "by_task": llm_provenance["by_task"],
            },
            "llm_daily_reports_with_unresolved_evidence": daily_reports_with_unresolved_evidence,
            "llm_daily_report_unresolved_evidence_items": daily_report_unresolved_evidence_items,
            "llm_daily_reports_without_evidence": daily_reports_without_evidence,
            "llm_daily_report_findings": daily_report_findings,
            "research_missing_source_links": research_missing_source_links,
            "research_invalid_source_links": research_invalid_source_links,
            "research_source_link_findings": research_source_link_findings,
            "conflicted_claims": conflicted_claims,
            "canonical": audit_canonical_database(db_path),
            "research": audit_research_database(
                db_path,
                root=root or db_path.parent.parent,
            ),
        }
    finally:
        conn.close()


def audit_project(root: Path) -> Dict[str, Any]:
    """建立不抓外網、不改業務資料的專案 provenance 報告。"""
    root = Path(root).resolve()
    db_report = _db_report(default_db_path(root), root=root)
    data_dir = root / "data"
    raw_dir = data_dir / "raw"
    registered = set(db_report.get("registered_content_paths") or [])
    raw_files = set(_relative_files(root, raw_dir))
    unregistered_raw = sorted(raw_files - registered)

    raw_archive = {
        "checked": 0,
        "missing": [],
        "mismatched": [],
        "hash_unavailable": [],
    }
    root_resolved = root.resolve()
    for manifest in db_report.get("document_manifest") or []:
        relative = str(manifest.get("content_path") or "").replace("\\", "/")
        expected = str(manifest.get("content_hash") or "").strip().lower()
        if not relative:
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            raw_archive["missing"].append(relative)
            continue
        if not candidate.is_file():
            raw_archive["missing"].append(relative)
            continue
        raw_archive["checked"] += 1
        if not expected:
            raw_archive["hash_unavailable"].append(relative)
            continue
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest != expected:
            raw_archive["mismatched"].append(relative)

    tables = db_report.get("tables") or {}
    gaps: List[Dict[str, Any]] = []
    canonical = db_report.get("canonical") or {}
    invalid_canonical = int(canonical.get("invalid_tables") or 0)
    partial_canonical = int(canonical.get("partial_tables") or 0)
    if invalid_canonical:
        gaps.append({
            "code": "canonical_semantic_invalid",
            "severity": "error",
            "message": (
                f"有 {invalid_canonical} 張 canonical table 通過保存但未通過語意資料檢查；"
                "請查看 database.canonical.tables 的 findings。"
            ),
            })
    elif partial_canonical:
        gaps.append({
            "code": "canonical_semantic_partial",
            "severity": "warning",
            "message": (
                f"有 {partial_canonical} 張 canonical table 有語意警告；"
                "請查看 database.canonical.tables 的 findings。"
            ),
        })
    research = db_report.get("research") or {}
    if research.get("quality_status") == "invalid":
        gaps.append({
            "code": "research_semantic_invalid",
            "severity": "error",
            "message": (
                f"研究文件稽核發現 {int(research.get('errors') or 0)} 個錯誤；"
                "請查看 database.research.findings，不得把這些內容當成已驗證證據。"
            ),
        })
    elif research.get("quality_status") == "partial":
        gaps.append({
            "code": "research_semantic_partial",
            "severity": "warning",
            "message": (
                f"研究文件稽核有 {int(research.get('warnings') or 0)} 個警告；"
                "回答時應保留來源版本與日期限制。"
            ),
        })
    source_runs_without_documents = int(
        db_report.get("source_runs_without_documents") or 0
    )
    if source_runs_without_documents:
        gaps.append({
            "code": "source_runs_without_raw_manifest",
            "severity": "warning",
            "message": (
                f"有 {source_runs_without_documents} 筆 source_runs 尚未關聯 "
                "source_documents；歷史來源尚未全面 raw-first。"
            ),
            "details": {
                "unresolved_source_runs": db_report.get("unresolved_source_runs") or [],
                "policy": "exact_only",
            },
        })
    llm_without_evidence = int(
        db_report.get("llm_analysis_without_evidence") or 0
    )
    if llm_without_evidence:
        gaps.append({
            "code": "llm_without_evidence_registry",
            "severity": "warning",
            "message": (
                f"有 {llm_without_evidence} 筆 LLM 分析尚未連結 evidence_ids；"
                "無法完整回溯其輸入證據。"
            ),
        })
    unresolved = int(
        db_report.get("llm_analysis_with_unresolved_evidence") or 0
    )
    if unresolved:
        gaps.append({
            "code": "llm_with_unresolved_evidence",
            "severity": "warning",
            "message": (
                f"有 {unresolved} 筆 LLM 分析引用了不存在或格式錯誤的 evidence_ids。"
            ),
        })
    daily_report_unresolved = int(
        db_report.get("llm_daily_reports_with_unresolved_evidence") or 0
    )
    if daily_report_unresolved:
        gaps.append({
            "code": "llm_daily_report_unresolved_evidence",
            "severity": "warning",
            "message": (
                f"有 {daily_report_unresolved} 筆每日報告含未驗證 evidence packet；"
                "不得把它們視為 exact composite report。"
            ),
            "details": {
                "unresolved_evidence_items": int(
                    db_report.get("llm_daily_report_unresolved_evidence_items") or 0
                ),
                "findings": db_report.get("llm_daily_report_findings") or [],
                "policy": "exact_only",
            },
        })
    daily_reports_without_evidence = int(
        db_report.get("llm_daily_reports_without_evidence") or 0
    )
    if daily_reports_without_evidence:
        gaps.append({
            "code": "llm_daily_report_without_evidence",
            "severity": "warning",
            "message": (
                f"有 {daily_reports_without_evidence} 筆每日報告標記為 ok 但沒有 evidence packet。"
            ),
        })
    research_missing_links = int(
        db_report.get("research_missing_source_links") or 0
    )
    research_invalid_links = int(
        db_report.get("research_invalid_source_links") or 0
    )
    if research_missing_links or research_invalid_links:
        gaps.append({
            "code": "research_evidence_source_link_gap",
            "severity": "error" if research_invalid_links else "warning",
            "message": (
                f"研究 evidence source link 缺口：{research_missing_links} 筆缺連結、"
                f"{research_invalid_links} 筆來源不存在或 raw 不可回放。"
            ),
            "details": {
                "missing_source_links": research_missing_links,
                "invalid_source_links": research_invalid_links,
                "findings": db_report.get("research_source_link_findings") or [],
                "policy": "exact_only",
            },
        })
    run_without_evidence = int(db_report.get("llm_runs_without_evidence") or 0)
    if run_without_evidence:
        gaps.append({
            "code": "research_llm_run_without_evidence",
            "severity": "warning",
            "message": f"有 {run_without_evidence} 筆 research LLM run 沒有可回溯 evidence link。",
        })
    partial_llm_runs = int((db_report.get("llm_run_provenance") or {}).get("counts", {}).get("partial", 0))
    if partial_llm_runs:
        gaps.append({
            "code": "research_llm_run_incomplete_evidence",
            "severity": "warning",
            "message": f"有 {partial_llm_runs} 筆 LLM run 的輸入證據與已保存連結不一致。",
        })
    conflicted_claims = int(db_report.get("conflicted_claims") or 0)
    if conflicted_claims:
        gaps.append({
            "code": "research_claim_conflict",
            "severity": "warning",
            "message": f"有 {conflicted_claims} 筆主張標記為 conflicted，不能直接升級長期記憶。",
        })
    integrity = db_report.get("integrity") or {}
    sqlite_integrity = integrity.get("sqlite") or {}
    if sqlite_integrity.get("status") == "failed":
        gaps.append({
            "code": "sqlite_integrity_failed",
            "severity": "error",
            "message": "SQLite PRAGMA integrity_check 未通過；停止後續寫入並先從備份還原檢查。",
            "details": sqlite_integrity.get("details") or [],
        })
    orphan_counts = integrity.get("orphan_counts") or {}
    orphan_total = sum(int(value or 0) for value in orphan_counts.values())
    if orphan_total:
        gaps.append({
            "code": "relational_orphan_links",
            "severity": "error",
            "message": (
                f"研究／記憶／canonical provenance 有 {orphan_total} 筆孤立連結："
                + ", ".join(
                    f"{name}={int(value)}"
                    for name, value in orphan_counts.items()
                    if int(value or 0)
                )
            ),
        })
    fts_inconsistent = [
        name for name, value in (integrity.get("fts") or {}).items()
        if value.get("consistent") is False
    ]
    if fts_inconsistent:
        gaps.append({
            "code": "fts_index_out_of_sync",
            "severity": "warning",
            "message": "FTS 索引筆數與 canonical table 不一致：" + ", ".join(fts_inconsistent),
        })
    if unregistered_raw:
        gaps.append({
            "code": "unregistered_raw_files",
            "severity": "warning",
            "message": f"有 {len(unregistered_raw)} 個 raw 檔案未出現在 source_documents manifest。",
        })
    if raw_archive["missing"]:
        gaps.append({
            "code": "raw_archive_missing",
            "severity": "error",
            "message": (
                f"有 {len(raw_archive['missing'])} 個 source_documents 指向不存在或越界的 raw 檔案。"
            ),
        })
    if raw_archive["mismatched"]:
        gaps.append({
            "code": "raw_archive_hash_mismatch",
            "severity": "error",
            "message": (
                f"有 {len(raw_archive['mismatched'])} 個 raw 檔案的 SHA-256 與 manifest 不一致。"
            ),
        })
    running = [
        item for item in db_report.get("source_health", [])
        if item.get("last_status") == "running"
    ]
    if running:
        gaps.append({
            "code": "source_left_running",
            "severity": "error",
            "message": f"有 {len(running)} 個來源最後狀態仍為 running，可能需要恢復或標記失敗。",
        })
    unhealthy = [
        item for item in db_report.get("source_health", [])
        if item.get("last_status") in {"failed", "invalid", "stale"}
        or int(item.get("consecutive_failures") or 0) > 0
    ]
    if unhealthy:
        gaps.append({
            "code": "source_health_not_healthy",
            "severity": "error",
            "message": f"有 {len(unhealthy)} 個來源最近一次執行失敗、過期或仍有連續失敗。",
        })
    running_ingestion = db_report.get("running_ingestion_runs") or []
    if running_ingestion:
        gaps.append({
            "code": "ingestion_runs_left_running",
            "severity": "error",
            "message": f"有 {len(running_ingestion)} 個 ingestion run 尚未完成。",
        })

    return {
        "command": "stock-data-audit",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "root": str(root),
        "database": db_report,
        "artifacts": {
            "raw": _file_summary(raw_dir),
            "news": _file_summary(data_dir / "news"),
            "pipeline_runs": _file_summary(data_dir / "pipeline_runs"),
            "llm_logs": _file_summary(root / "log" / "llm_calls"),
        },
        "raw_archive": raw_archive,
        "unregistered_raw_files": unregistered_raw[:1000],
        "known_limits": [
            {
                "code": "legacy_llm_without_provable_evidence",
                "severity": "info",
                "message": (
                    f"有 {int(db_report.get('llm_analysis_legacy_unresolved') or 0)} 筆舊 LLM 分析 "
                    "已明確標記 legacy_unresolved；保留原文但不冒充有證據鏈。"
                ),
            }
        ] if int(db_report.get("llm_analysis_legacy_unresolved") or 0) else [],
        "gap_count": len(gaps),
        "gaps": gaps,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-data-audit",
        description="只讀盤點 SQLite、raw、cache、pipeline 與 LLM provenance 缺口",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    parser.add_argument(
        "--fail-on-gap",
        action="store_true",
        help="有任何 provenance 缺口時回傳 exit code 2",
    )
    args = parser.parse_args(argv)
    report = audit_project(args.root)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        db = report["database"]
        print(f"stock-data-audit: {report['root']}")
        print(
            f"DB: {db['path']} (exists={db['exists']}, "
            f"schema_compat={db['schema_version']}, "
            f"latest_schema={db.get('latest_schema_version', db['schema_version'])})"
        )
        for table, count in (db.get("tables") or {}).items():
            print(f"  {table}: {count}")
        canonical = db.get("canonical") or {}
        print(
            "canonical semantic audit: "
            f"{canonical.get('rows_checked', 0)} rows, "
            f"{canonical.get('invalid_tables', 0)} invalid tables, "
            f"{canonical.get('partial_tables', 0)} partial tables, "
            f"{canonical.get('empty_tables', 0)} empty tables"
        )
        print(f"raw files: {report['artifacts']['raw']['files']}")
        if report["gaps"]:
            print("gaps:")
            for gap in report["gaps"]:
                print(f"  [{gap['severity']}] {gap['code']}: {gap['message']}")
        else:
            print("gaps: none")
    return 2 if args.fail_on_gap and report["gaps"] else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["audit_project", "main"]

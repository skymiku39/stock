"""Freeze exact historical gaps; new findings remain actionable."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

from bot.stock_db import StockDB, default_db_path
from bot.utils import now_tw


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def load_exclusions(root: Path) -> dict:
    path = default_db_path(root)
    if not path.exists():
        return {}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='historical_exclusions'").fetchone():
            return {}
        return {(r[0], r[1], r[2]): json.loads(r[3]) for r in conn.execute(
            "SELECT kind,record_id,fingerprint,payload_json FROM historical_exclusions")}
    finally:
        conn.close()


def apply_archive(root: Path, *, apply=False) -> dict:
    from bot.source_audit_cli import audit_project
    root = Path(root).resolve()
    audit = audit_project(root, include_archived=True)
    rows = []
    for item in audit["database"].get("llm_evidence_review", {}).get("records", []):
        rows.append((item["record_type"], item["record_id"], item["snapshot_hash"], item))
    for item in audit["database"].get("unresolved_source_runs", []):
        rows.append(("source_run", item["run_id"], fingerprint(item), item))
    for relative in audit["unregistered_raw_files"]:
        path = (root / relative).resolve()
        path.relative_to(root / "data" / "raw")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(("raw", relative, digest, {"path": relative, "sha256": digest}))
    # Only individually identified, already quarantined research is archived.
    # Version conflicts and aggregate semantic findings remain visible.
    for item in audit["database"].get("research", {}).get("findings", []):
        if item["code"] == "source_type_quarantined" and item.get("document_id"):
            rows.append(("research", item["document_id"], fingerprint(item), item))
    counts = {kind: sum(r[0] == kind for r in rows) for kind in sorted({r[0] for r in rows})}
    result = {"dry_run": not apply, "counts": counts, "policy": "exact_historical_records_only"}
    if apply:
        db = StockDB.open(root=root)
        try:
            with db.transaction() as conn:
                for kind, record_id, digest, payload in rows:
                    conn.execute("INSERT OR IGNORE INTO historical_exclusions VALUES (?,?,?,?,?)",
                                 (kind, record_id, digest, json.dumps(payload, ensure_ascii=False), now_tw().isoformat()))
                conn.execute("INSERT OR IGNORE INTO historical_exclusions VALUES ('policy','trusted_research_only','v1','{}',?)",
                             (now_tw().isoformat(),))
        finally:
            db.close()
    return result


def partition_audit(root: Path, database: dict, raw_files: list[str]) -> tuple[dict, list[str]]:
    """Adjust active counts only for identical archived findings, never by date."""
    excluded = load_exclusions(root)
    archive = {"llm": [], "source_runs": [], "raw": [], "research": []}
    if not excluded:
        return archive, raw_files
    review = database.get("llm_evidence_review") or {}
    active = []
    for item in review.get("records", []):
        key = (item["record_type"], item["record_id"], item["snapshot_hash"])
        if key in excluded:
            archive["llm"].append(item)
        else:
            active.append(item)
    review["archived"] = len(archive["llm"])
    review["records"] = active
    review["unresolved"] = len(active)
    review["marked"] = sum(r["marked"] for r in active)
    grouped = Counter((r["record_type"], r["task"], r["execution_status"], r["origin"]) for r in active)
    review["by_path"] = [dict(record_type=k[0], task=k[1], execution_status=k[2], origin=k[3], count=v)
                         for k, v in sorted(grouped.items())]
    provenance = database.get("llm_run_provenance") or {}
    archived_analyses = {r["record_id"] for r in archive["llm"] if r["record_type"] == "analysis"}
    archived_runs = {r["record_id"] for r in archive["llm"] if r["record_type"] == "run"}
    for category, field in (("without_evidence", "llm_analysis_without_evidence"),
                            ("unresolved_evidence", "llm_analysis_with_unresolved_evidence")):
        ids = database.get("llm_analysis_gap_ids", {}).get(category, [])
        database[field] = len(set(ids) - archived_analyses)
    if "findings" in provenance:
        current = [r for r in provenance["findings"] if r["run_id"] not in archived_runs]
        provenance["findings"] = current
        provenance["counts"] = {category: sum(r["category"] == category for r in current)
                                for category in provenance["counts"]}
        groups = Counter((r["task_type"], r["execution_status"], r["category"]) for r in current)
        provenance["by_task"] = [dict(task_type=k[0], execution_status=k[1], evidence_category=k[2], count=v)
                                 for k, v in sorted(groups.items())]
        database["llm_runs_without_evidence"] = sum(
            not r["has_evidence"] and r["category"] not in {"failed", "not_required", "legacy_unresolved"}
            for r in current)
    remaining = []
    for item in database.get("unresolved_source_runs", []):
        if ("source_run", item["run_id"], fingerprint(item)) in excluded:
            archive["source_runs"].append(item)
        else:
            remaining.append(item)
    database["unresolved_source_runs"] = remaining
    database["source_runs_without_documents"] = len(remaining)
    database["source_runs_without_documents_by_status"] = {
        status: sum(r["status"] == status for r in remaining) for status in {r["status"] for r in remaining}}
    active_raw = []
    for relative in raw_files:
        path = (root / relative).resolve()
        try:
            path.relative_to((root / "data" / "raw").resolve())
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError):
            digest = ""
        (archive["raw"] if ("raw", relative, digest) in excluded else active_raw).append(relative)
    research = database.get("research") or {}
    findings = []
    for item in research.get("findings", []):
        if ("research", item.get("document_id", ""), fingerprint(item)) in excluded:
            archive["research"].append(item)
        else:
            findings.append(item)
    research["findings"] = findings
    research["warnings"] = sum(r["severity"] == "warning" for r in findings)
    research["quality_status"] = "invalid" if research.get("errors") else "partial" if research["warnings"] else "ok"
    return archive, active_raw


def cache_is_trusted(root: Path, data: dict) -> bool:
    """After opt-in, normal cache reads require complete replayable evidence."""
    if ("policy", "trusted_research_only", "v1") not in load_exclusions(root):
        return True
    from bot.auto_llm import _select_verified_long_memory_evidence
    sections = data.get("source_sections")
    sections = sections if isinstance(sections, dict) else {}
    packets = data.get("evidence") or sections.get("evidence") or []
    if not packets or data.get("validation_errors"):
        return False
    from types import SimpleNamespace
    path = default_db_path(root)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    def get_row(table, key, value):
        row = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
        return SimpleNamespace(**dict(row)) if row else None
    db = SimpleNamespace(path=path,
                         get_evidence=lambda value: get_row("evidence_items", "evidence_id", value),
                         get_source_document=lambda value: get_row("source_documents", "document_id", value))
    try:
        accepted, rejected = _select_verified_long_memory_evidence(db, packets, root=root)
        if not accepted or rejected:
            return False
        for packet in accepted:
            evidence = db.get_evidence(packet["id"])
            if evidence.quality_status not in {"ok", "partial"}:
                return False
            metadata = json.loads(evidence.metadata_json or "{}")
            if not isinstance(metadata, dict) or metadata.get("provenance_status") not in {"exact", "composite_exact"}:
                return False
            if evidence.source_document_id and evidence.source_document_id != packet.get("source_document_id"):
                return False
            documents = set(metadata.get("source_document_ids") or [])
            if evidence.source_document_id:
                documents.add(evidence.source_document_id)
            if not documents:
                return False
            from bot.source_registry import is_source_document_replayable
            for document_id in documents:
                source = db.get_source_document(document_id)
                if source is None or source.quality_status not in {"ok", "partial"} or not is_source_document_replayable(root, source):
                    return False
        return True
    except (ValueError, TypeError, sqlite3.Error):
        return False
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Archive exact historical gaps; preserve originals and new alerts")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(apply_archive(args.root, apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

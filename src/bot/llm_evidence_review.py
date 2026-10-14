"""Explicit unresolved labels for historical outputs, without invented links."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

from bot.llm_provenance import evidence_ids
from bot.stock_db import StockDB, default_db_path
from bot.utils import now_tw


def _object(value):
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return None


def review_plan(conn: sqlite3.Connection) -> dict:
    """Recompute findings; a label only applies to the exact reviewed snapshot."""
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    known = {r[0] for r in conn.execute("SELECT evidence_id FROM evidence_items")} if "evidence_items" in tables else set()
    marked = {tuple(r) for r in conn.execute("SELECT record_type,record_id,snapshot_hash FROM llm_evidence_reviews")} if "llm_evidence_reviews" in tables else set()
    links: dict[str, set[str]] = {}
    if "llm_run_evidence" in tables:
        for row in conn.execute("SELECT run_id,evidence_id FROM llm_run_evidence"):
            links.setdefault(row[0], set()).add(row[1])
    findings = []
    planners = set()
    groups = Counter()

    def add(kind, row, record_id, task, execution, origin, reason, actual=(), expected=()):
        snapshot = {"record": dict(row), "links": sorted(actual),
                    "expected": sorted(expected),
                    "registered_ids": sorted((set(actual) | set(expected)) & known)}
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        findings.append({"record_type": kind, "record_id": record_id,
                         "task": task, "execution_status": execution,
                         "origin": origin, "classification": "unverified",
                         "reason": reason, "snapshot_hash": digest,
                         "marked": (kind, record_id, digest) in marked})
        groups[(kind, task, execution, origin)] += 1

    if "llm_runs" in tables:
        for row in conn.execute("SELECT * FROM llm_runs ORDER BY run_id"):
            md = _object(row["metadata_json"])
            md = md if isinstance(md, dict) else {}
            expected = set(evidence_ids(md.get("evidence_ids")))
            actual = links.get(row["run_id"], set())
            receipt = md.get("input_evidence") or {}
            if (row["task_type"] == row["prompt_id"] == "query_plan"
                    and isinstance(receipt, dict) and receipt.get("state") == "not_required"
                    and not expected and not actual):
                if md.get("analysis_id"):
                    planners.add(md["analysis_id"])
                continue
            if actual and actual <= known and (not expected or expected == actual):
                continue
            reason = "input_evidence_missing" if not actual else "input_evidence_incomplete"
            origin = "call_id" if md.get("call_id") else (
                "legacy_import" if row["run_id"].startswith("legacy-analysis:") else "historical_or_unknown")
            add("run", row, row["run_id"], row["task_type"], row["status"], origin, reason, actual, expected)
    if "llm_analysis_history" in tables:
        for row in conn.execute("SELECT * FROM llm_analysis_history ORDER BY id"):
            if row["id"] in planners:
                continue
            values = _object(row["evidence_json"])
            ids = evidence_ids(values)
            if isinstance(values, list) and ids and len(ids) == len(values) and set(ids) <= known:
                continue
            add("analysis", row, row["id"], row["prompt_id"],
                "ok" if row["success"] else "failed", "analysis_history",
                "input_evidence_missing" if not ids else "input_evidence_incomplete", ids)
    return {"records": findings, "unresolved": len(findings),
            "marked": sum(r["marked"] for r in findings),
            "by_path": [dict(record_type=k[0], task=k[1], execution_status=k[2], origin=k[3], count=v)
                        for k, v in sorted(groups.items())]}


def review_history(root: Path, *, apply=False) -> dict:
    root = Path(root).resolve()
    path = default_db_path(root)
    if not path.exists():
        return {"dry_run": not apply, "unresolved": 0, "marked": 0, "records": [], "by_path": [], "applied": 0}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        report = review_plan(conn)
    finally:
        conn.close()
    report.update(dry_run=not apply, applied=0)
    if not apply:
        return report
    db = StockDB.open(root=root)
    try:
        with db.transaction() as conn:
            # Plan under the write transaction so concurrent updates are never
            # marked using a stale preview. Raw output/status stays untouched.
            report = review_plan(conn)
            report.update(dry_run=False, applied=0)
            for item in report["records"]:
                if not item["marked"]:
                    conn.execute("INSERT INTO llm_evidence_reviews VALUES (?,?,?,?,?,?)",
                                 (item["record_type"], item["record_id"], item["snapshot_hash"],
                                  item["classification"], item["reason"], now_tw().isoformat()))
                    report["applied"] += 1
                if item["record_type"] == "run":
                    conn.execute("UPDATE llm_outputs SET is_publishable=0 WHERE run_id=?",
                                 (item["record_id"],))
                item["marked"] = True
            report["marked"] = len(report["records"])
    finally:
        db.close()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Preview historical LLM evidence gaps; explicitly mark unverified with --apply")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(review_history(args.root, apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

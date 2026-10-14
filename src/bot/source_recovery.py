"""Durable registration receipts; replay never infers historical provenance."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from bot.stock_db import SourceDocumentRow, StockDB, default_db_path


def write_receipt(root: Path, row: SourceDocumentRow) -> str:
    payload = json.dumps({"version": 1, "document": asdict(row)},
                         ensure_ascii=False, sort_keys=True).encode("utf-8")
    receipt_id = hashlib.sha256(payload).hexdigest()
    directory = root / "data" / "source_registration_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{receipt_id}.json"
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("registration receipt content mismatch")
        return receipt_id
    fd, name = tempfile.mkstemp(dir=directory, suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return receipt_id


def commit_registration(db: StockDB, receipt_id: str, row: SourceDocumentRow,
                        *, replay: bool = False) -> None:
    """Manifest, exact run links and acknowledgement share one transaction.

    Replay preserves an existing manifest and all run statuses/errors/dates.
    A receipt acknowledges registration, not successful parsing or ingestion.
    """
    with db.transaction() as conn:
        if conn.execute("SELECT 1 FROM source_registration_receipts WHERE receipt_id=?",
                        (receipt_id,)).fetchone():
            return
        existing = db.get_source_document(row.document_id)
        if existing and existing.content_hash != row.content_hash:
            raise ValueError("manifest hash conflict")
        if not replay or existing is None:
            db.upsert_source_document(row)
        if row.ingestion_run_id:
            run = db.get_ingestion_run(row.ingestion_run_id)
            if run is None or run.source != row.source:
                raise ValueError("registration ingestion is missing or mismatched")
            for table, column, run_id in (
                ("ingestion_runs", "metadata_json", run.run_id),
                ("source_runs", "details_json", run.source_run_id or run.run_id),
            ):
                stored = conn.execute(f"SELECT {column} FROM {table} WHERE run_id=?",
                                      (run_id,)).fetchone()
                if stored is None:
                    raise ValueError(f"registration {table} link is missing")
                metadata = json.loads(stored[0] or "{}")
                ids = metadata.setdefault("document_ids", [])
                if not isinstance(ids, list):
                    raise ValueError("invalid document_ids metadata")
                if row.document_id not in ids:
                    ids.append(row.document_id)
                conn.execute(f"UPDATE {table} SET {column}=? WHERE run_id=?",
                             (json.dumps(metadata, ensure_ascii=False), run_id))
        conn.execute("INSERT INTO source_registration_receipts VALUES (?,?,?)",
                     (receipt_id, row.document_id, row.ingestion_run_id))


def recover_registrations(root: Path, *, apply: bool = False) -> dict:
    """Preview uses SQLite mode=ro and never initializes schema or cloud cache."""
    root = Path(root).resolve()
    path = default_db_path(root)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) if path.exists() else None
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} if conn else set()
    acknowledged = {r[0] for r in conn.execute("SELECT receipt_id FROM source_registration_receipts")} if "source_registration_receipts" in tables else set()
    registered = {str(r[0]).replace("\\", "/") for r in conn.execute("SELECT content_path FROM source_documents")} if "source_documents" in tables else set()
    report = {"dry_run": not apply, "receipts": [], "applied": 0,
              "unresolved_raw": [], "policy": "exact_receipt_and_hash_only"}
    provable_paths: set[str] = set()
    candidates = []
    try:
        for receipt in sorted((root / "data" / "source_registration_receipts").glob("*.json")):
            item = {"receipt_id": receipt.stem, "state": "unresolved"}
            try:
                payload = receipt.read_bytes()
                if hashlib.sha256(payload).hexdigest() != receipt.stem:
                    raise ValueError("receipt hash mismatch")
                data = json.loads(payload)
                if data.get("version") != 1:
                    raise ValueError("unsupported receipt version")
                row = SourceDocumentRow(**data["document"])
                from bot.source_registry import _safe_part, is_source_document_replayable
                if row.document_id != f"source-doc:{_safe_part(row.source)}:{row.content_hash}":
                    raise ValueError("document identity mismatch")
                raw_path = (root / row.content_path).resolve()
                raw_path.relative_to((root / "data" / "raw").resolve())
                if not row.retrieved_at or not is_source_document_replayable(root, row):
                    raise ValueError("raw missing, hash mismatch, or retrieval date absent")
                if row.ingestion_run_id:
                    ingestion = conn.execute("SELECT source, source_run_id FROM ingestion_runs WHERE run_id=?",
                                             (row.ingestion_run_id,)).fetchone() if conn else None
                    if not ingestion or ingestion[0] != row.source:
                        raise ValueError("ingestion missing or mismatched")
                    source_run = conn.execute("SELECT source FROM source_runs WHERE run_id=?",
                                              (ingestion[1] or row.ingestion_run_id,)).fetchone()
                    if not source_run or source_run[0] != row.source:
                        raise ValueError("source run missing or mismatched")
                existing = conn.execute("SELECT content_hash FROM source_documents WHERE document_id=?",
                                        (row.document_id,)).fetchone() if "source_documents" in tables else None
                if existing and existing[0] != row.content_hash:
                    raise ValueError("manifest hash conflict")
                provable_paths.add(row.content_path.replace("\\", "/"))
                item.update(document_id=row.document_id, ingestion_run_id=row.ingestion_run_id,
                            state="registered" if receipt.stem in acknowledged else "recoverable")
                if item["state"] == "recoverable":
                    candidates.append((item, row))
            except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
                item["reason"] = str(exc)
            report["receipts"].append(item)
    finally:
        if conn:
            conn.close()
    if apply and candidates:
        db = StockDB.open(root=root)
        try:
            for item, row in candidates:
                try:
                    from bot.source_registry import is_source_document_replayable
                    if not is_source_document_replayable(root, row):
                        raise ValueError("raw changed after preview")
                    commit_registration(db, item["receipt_id"], row, replay=True)
                    item["state"] = "applied"
                    registered.add(row.content_path.replace("\\", "/"))
                    report["applied"] += 1
                except (ValueError, TypeError, sqlite3.Error) as exc:
                    item.update(state="unresolved", reason=str(exc))
        finally:
            db.close()
    report["unresolved_raw"] = sorted(str(p.relative_to(root)).replace("\\", "/")
                                      for p in (root / "data" / "raw").rglob("*")
                                      if p.is_file() and str(p.relative_to(root)).replace("\\", "/")
                                      not in registered | provable_paths)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Preview exact raw registration recovery; no source inference")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    report = recover_registrations(args.root, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["unresolved_raw"] or any(r["state"] == "unresolved" for r in report["receipts"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())

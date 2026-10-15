import json
import sqlite3

import pytest

from bot.source_registry import SourceRegistry
from bot.source_recovery import recover_registrations
from bot.stock_db import StockDB


def test_failed_manifest_survives_restart_and_replays_once(tmp_path, monkeypatch):
    db = StockDB.open(root=tmp_path)
    registry = SourceRegistry(tmp_path, db)
    with monkeypatch.context() as patch:
        def fail(row):
            raise sqlite3.OperationalError("injected database is locked")
        patch.setattr(db, "upsert_source_document", fail)
        with pytest.raises(sqlite3.OperationalError):
            registry.capture_document("fixture", b"raw", source_url="https://example.test",
                                      source_ref="original", as_of="2026-01-01",
                                      retrieved_at="2026-01-02T12:00:00+08:00", run_id="ingest:test")
    assert db.list_source_documents() == []
    assert db.get_ingestion_run("ingest:test").status == "failed"
    db.close()
    before = (tmp_path / "data" / "stock.db").read_bytes()
    preview = recover_registrations(tmp_path)
    assert preview["receipts"][0]["state"] == "recoverable"
    assert (tmp_path / "data" / "stock.db").read_bytes() == before
    assert recover_registrations(tmp_path, apply=True)["applied"] == 1
    assert recover_registrations(tmp_path, apply=True)["applied"] == 0
    db = StockDB.open(root=tmp_path)
    doc, = db.list_source_documents()
    assert (doc.as_of, doc.retrieved_at, doc.source_ref) == ("2026-01-01", "2026-01-02T12:00:00+08:00", "original")
    run = db.get_ingestion_run("ingest:test")
    assert run.status == "failed" and "locked" in run.error
    assert json.loads(run.metadata_json)["document_ids"] == [doc.document_id]
    assert json.loads(db.list_source_runs()[0].details_json)["document_ids"] == [doc.document_id]
    db.close()


@pytest.mark.parametrize("fault", ["hash", "receipt", "missing", "outside"])
def test_recovery_rejects_unprovable_raw(tmp_path, monkeypatch, fault):
    db = StockDB.open(root=tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(db, "upsert_source_document", lambda row: (_ for _ in ()).throw(ValueError("fail")))
        with pytest.raises(ValueError):
            SourceRegistry(tmp_path, db).register_document("fixture", b"raw")
    db.close()
    raw, = (tmp_path / "data" / "raw").rglob("*.bin")
    receipt, = (tmp_path / "data" / "source_registration_receipts").glob("*.json")
    if fault == "hash":
        raw.write_bytes(b"changed")
    elif fault == "missing":
        raw.unlink()
    elif fault == "receipt":
        receipt.write_text("{}")
    else:
        import hashlib
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["document"]["content_path"] = "../outside.bin"
        payload = json.dumps(data).encode()
        receipt.unlink()
        receipt.with_name(hashlib.sha256(payload).hexdigest() + ".json").write_bytes(payload)
    report = recover_registrations(tmp_path, apply=True)
    assert report["applied"] == 0
    assert report["receipts"][0]["state"] == "unresolved"


def test_partial_link_failure_rolls_back_manifest_and_keeps_receipt(tmp_path):
    db = StockDB.open(root=tmp_path)
    registry = SourceRegistry(tmp_path, db)
    run = registry.start_ingestion("fixture")
    db.conn.execute("CREATE TRIGGER fail_link BEFORE UPDATE ON source_runs BEGIN SELECT RAISE(ABORT, 'injected link failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        registry.register_document("fixture", b"raw", ingestion_run_id=run.run_id)
    assert db.list_source_documents() == []
    assert db.conn.execute("SELECT count(*) FROM source_registration_receipts").fetchone()[0] == 0
    assert "document_ids" not in json.loads(db.get_ingestion_run(run.run_id).metadata_json)
    assert recover_registrations(tmp_path, apply=True)["applied"] == 0
    db.conn.execute("DROP TRIGGER fail_link")
    db.close()
    assert recover_registrations(tmp_path, apply=True)["applied"] == 1


def test_historical_orphan_is_never_inferred_and_preview_does_not_create_db(tmp_path):
    raw = tmp_path / "data" / "raw" / "fixture.bin"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"orphan")
    report = recover_registrations(tmp_path)
    assert report["unresolved_raw"] == ["data/raw/fixture.bin"]
    assert not (tmp_path / "data" / "stock.db").exists()


def test_replay_does_not_overwrite_newer_manifest(tmp_path, monkeypatch):
    db = StockDB.open(root=tmp_path)
    registry = SourceRegistry(tmp_path, db)
    with monkeypatch.context() as patch:
        patch.setattr(db, "upsert_source_document", lambda row: (_ for _ in ()).throw(ValueError("fail")))
        with pytest.raises(ValueError):
            registry.register_document("fixture", b"raw", as_of="2020-01-01")
    doc = registry.register_document("fixture", b"raw", as_of="2026-01-01")
    assert recover_registrations(tmp_path, apply=True)["applied"] == 1
    assert db.get_source_document(doc.document_id).as_of == "2026-01-01"
    db.close()


def test_receipt_failure_prevents_raw_publication(tmp_path, monkeypatch):
    db = StockDB.open(root=tmp_path)
    def fail(*args):
        raise OSError("receipt disk full")
    monkeypatch.setattr("bot.source_recovery.write_receipt", fail)
    with pytest.raises(OSError):
        SourceRegistry(tmp_path, db).register_document("fixture", b"raw")
    assert not list((tmp_path / "data" / "raw").rglob("*.bin"))
    assert db.list_source_documents() == []
    db.close()


def test_conflicting_pending_receipts_never_choose_a_date_by_hash_order(tmp_path, monkeypatch):
    db = StockDB.open(root=tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(db, "upsert_source_document", lambda row: (_ for _ in ()).throw(ValueError("fail")))
        for date in ("2020-01-01", "2026-01-01"):
            with pytest.raises(ValueError):
                SourceRegistry(tmp_path, db).register_document("fixture", b"raw", as_of=date)
    report = recover_registrations(tmp_path, apply=True)
    assert report["applied"] == 0
    assert len(report["unresolved_raw"]) == 1
    assert all(row["state"] == "unresolved" for row in report["receipts"])
    assert db.list_source_documents() == []
    db.close()


def test_registration_reserves_writer_before_reading_manifest(tmp_path, monkeypatch):
    from bot.source_recovery import commit_registration
    from bot.stock_db import SourceDocumentRow
    db = StockDB.open(root=tmp_path)
    other = sqlite3.connect(db.path, timeout=0.01)
    original = db.get_source_document
    blocked = []
    def read(document_id):
        # A concurrent process cannot commit between the manifest read and write.
        try:
            other.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            blocked.append(str(exc))
        else:
            other.rollback()
        return original(document_id)
    monkeypatch.setattr(db, "get_source_document", read)
    try:
        row = SourceDocumentRow(document_id="test-lock", source="fixture", content_hash="hash")
        commit_registration(db, "receipt", row)
        assert blocked and all("locked" in message for message in blocked)
        assert original(row.document_id).content_hash == "hash"
    finally:
        other.close()
        db.close()

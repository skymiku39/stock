from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bot.source_registry import (
    SourceRegistry,
    is_source_document_replayable,
    resolve_source_document_path,
)
from bot.source_audit_cli import audit_project
from bot.stock_db import StockDB


def test_source_registry_writes_hash_addressed_immutable_document(tmp_path: Path) -> None:
    db = StockDB.open(path=tmp_path / "registry.db")
    registry = SourceRegistry(tmp_path, db)
    run = registry.start_ingestion("mops", source_url="https://example.test/mops")
    doc = registry.register_document(
        "mops",
        "公告原文 2330",
        kind="announcement",
        source_ref="公告-1",
        url="https://example.test/mops/1",
        published_at="2026-09-16T09:00:00+08:00",
        ingestion_run_id=run.run_id,
        suffix="txt",
        metadata={"ticker": "2330"},
    )
    path = tmp_path / doc.content_path
    original = path.read_bytes()
    same = registry.register_document("mops", "公告原文 2330", suffix="txt")
    assert same.document_id == doc.document_id
    assert path.read_bytes() == original
    assert db.get_source_document(doc.document_id).content_hash == doc.content_hash
    assert db.get_source_document(doc.document_id).ingestion_run_id == run.run_id

    finished = registry.finish_ingestion(run.run_id, status="ok", document_count=1)
    assert finished is not None
    assert finished.status == "ok"
    assert finished.document_count == 1
    assert db.list_source_documents(source="mops")
    assert finished.source_run_id == run.run_id
    source_runs = db.list_source_runs(source="mops")
    assert len(source_runs) == 1
    assert source_runs[0].run_id == run.run_id
    assert source_runs[0].status == "ok"
    health = db.get_source_health("mops")
    assert health is not None
    assert health.last_status == "ok"


def test_source_registry_changes_document_id_when_content_changes(tmp_path: Path) -> None:
    db = StockDB.open(path=tmp_path / "registry.db")
    registry = SourceRegistry(tmp_path, db)
    first = registry.register_document("news", "第一版", suffix="html")
    second = registry.register_document("news", "第二版", suffix="html")
    assert first.document_id != second.document_id
    assert first.content_hash != second.content_hash
    assert len(db.list_source_documents(source="news")) == 2


def test_source_document_replayability_requires_existing_unchanged_raw_file(
    tmp_path: Path,
) -> None:
    with SourceRegistry(tmp_path) as registry:
        document = registry.register_document(
            "fixture",
            b"immutable raw fixture",
            suffix="txt",
        )

    assert is_source_document_replayable(tmp_path, document) is True
    raw_path = tmp_path / document.content_path
    raw_path.write_bytes(b"tampered raw fixture")
    assert is_source_document_replayable(tmp_path, document) is False

    raw_path.unlink()
    assert is_source_document_replayable(tmp_path, document) is False

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"immutable raw fixture")
    invalid = document.__class__(
        **{
            **document.__dict__,
            "content_path": "../outside.txt",
        }
    )
    assert is_source_document_replayable(tmp_path, invalid) is False


def test_capture_document_closes_ingestion_run_and_is_idempotent_by_content(tmp_path: Path) -> None:
    db = StockDB.open(path=tmp_path / "data" / "stock.db")
    registry = SourceRegistry(tmp_path, db)

    first = registry.capture_document(
        "macro",
        b"{\"asof\":\"2026-09-17\"}",
        source_url="yfinance://macro",
        source_ref="macro:2026-09-17",
        title="Macro snapshot",
        kind="snapshot",
        suffix="json",
        mime_type="application/json",
        as_of="2026-09-17",
    )
    second = registry.capture_document(
        "macro",
        b"{\"asof\":\"2026-09-17\"}",
        source_url="yfinance://macro",
        source_ref="macro:2026-09-17",
        title="Macro snapshot",
        kind="snapshot",
        suffix="json",
        mime_type="application/json",
        as_of="2026-09-17",
    )

    assert first.document_id == second.document_id
    assert len(db.list_source_documents(source="macro")) == 1
    runs = db.list_ingestion_runs(source="macro")
    assert len(runs) == 2
    assert all(run.status == "ok" for run in runs)
    assert db.get_source_health("macro").last_status == "ok"
    assert audit_project(tmp_path)["database"]["source_runs_without_documents"] == 0


def test_concurrent_short_lived_captures_do_not_lose_raw_manifest_rows(
    tmp_path: Path,
) -> None:
    """!ask 的並行來源 adapter 不應因各自開 DB 而觸發 SQLite lock。"""

    def capture(index: int) -> str:
        registry = SourceRegistry(tmp_path)
        return registry.capture_document(
            "parallel_fixture",
            f"raw payload {index}".encode("utf-8"),
            source_ref=f"parallel:{index}",
            suffix="txt",
        ).document_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        document_ids = list(pool.map(capture, range(24)))

    db = StockDB.open(root=tmp_path)
    try:
        assert len(document_ids) == 24
        assert len(set(document_ids)) == 24
        documents = db.list_source_documents(source="parallel_fixture")
        assert len(documents) == 24
        assert all(row.content_hash for row in documents)
        assert all(run.status == "ok" for run in db.list_ingestion_runs(
            source="parallel_fixture",
        ))
    finally:
        db.close()


def test_owned_capture_closes_sqlite_handle_on_windows(tmp_path: Path) -> None:
    """短生命週期 raw capture 後可立即搬移 DB，不留下 Windows lock。"""
    db_path = tmp_path / "data" / "stock.db"
    SourceRegistry(tmp_path).capture_document(
        "fixture", b"raw", source_ref="fixture:close", suffix="bin",
    )

    moved = tmp_path / "data" / "stock-moved.db"
    db_path.rename(moved)
    assert moved.is_file()


def test_raw_archive_mirrors_and_restores_from_cloud_cache(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "project"
    cloud = tmp_path / "cloud-cache"
    monkeypatch.setenv("GOOGLE_CACHE_DIR", str(cloud))

    with SourceRegistry(project) as registry:
        document = registry.register_document(
            "fixture",
            b"immutable raw fixture",
            suffix="json",
        )

    local_path = project / document.content_path
    mirrored_path = cloud / document.content_path
    assert local_path.is_file()
    assert mirrored_path.read_bytes() == b"immutable raw fixture"

    local_path.unlink()
    restored = resolve_source_document_path(project, document)
    assert restored == local_path
    assert restored.read_bytes() == b"immutable raw fixture"

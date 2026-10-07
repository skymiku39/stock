"""Issue #5：watchlist 不得以過期 JSON 整批刪除 SQLite 雲端列。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot import watchlist as wl
from bot.stock_db import StockDB, WatchlistRow, default_db_path, reset_db_singleton


@pytest.fixture(autouse=True)
def _reset_db_singleton() -> None:
    reset_db_singleton()
    yield
    reset_db_singleton()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔離專案 root，並 mock 雲端檔案 restore／mirror。"""
    monkeypatch.setattr(wl, "restore_file_from_cloud", lambda *a, **k: False)
    monkeypatch.setattr(wl, "mirror_file_to_cloud", lambda *a, **k: False)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_json(root: Path, items: list[dict]) -> None:
    path = root / "data" / "watchlist.json"
    path.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _db_symbols(root: Path) -> set[str]:
    db = StockDB.open(path=default_db_path(root))
    try:
        return {row.symbol for row in db.list_watchlist()}
    finally:
        db.close()


def _seed_cloud_only_and_local(root: Path) -> None:
    """SQLite 含雲端列 CLOUD1；JSON 僅有過期的本機列 2330。"""
    _write_json(
        root,
        [
            {
                "ticker": "2330",
                "name": "台積電",
                "tags": [],
                "note": "local",
                "added_at": "2026-01-01T00:00:00+08:00",
            }
        ],
    )
    db = StockDB.open(path=default_db_path(root))
    try:
        db.upsert_watch(
            WatchlistRow(
                symbol="2330",
                name="台積電",
                note="local",
                added_at="2026-01-01T00:00:00+08:00",
            )
        )
        db.upsert_watch(
            WatchlistRow(
                symbol="CLOUD1",
                name="雲端同步",
                tags="cloud",
                note="pulled",
                added_at="2026-01-02T00:00:00+08:00",
            )
        )
    finally:
        db.close()


def test_update_preserves_cloud_only_sqlite_row(root: Path) -> None:
    """更新另一筆既有列時，不得刪除只存在於 SQLite 的雲端列。"""
    _seed_cloud_only_and_local(root)

    wl.update("2330", note="edited", root=root)

    assert "CLOUD1" in _db_symbols(root)
    assert "2330" in _db_symbols(root)
    loaded = {item.ticker: item for item in wl.load(root).items}
    assert "CLOUD1" in loaded
    assert loaded["2330"].note == "edited"


def test_remove_only_requested_symbol_preserves_cloud_row(root: Path) -> None:
    """明確移除一筆時，只刪該 symbol，並保留其他 SQLite 列（含雲端列）。"""
    _seed_cloud_only_and_local(root)
    db = StockDB.open(path=default_db_path(root))
    try:
        db.upsert_watch(
            WatchlistRow(
                symbol="2317",
                name="鴻海",
                added_at="2026-01-01T01:00:00+08:00",
            )
        )
    finally:
        db.close()
    # 過期 JSON 故意不含 CLOUD1，模擬雲端 pull 後尚未回寫 JSON
    _write_json(
        root,
        [
            {
                "ticker": "2330",
                "name": "台積電",
                "tags": [],
                "note": "local",
                "added_at": "2026-01-01T00:00:00+08:00",
            },
            {
                "ticker": "2317",
                "name": "鴻海",
                "tags": [],
                "note": "",
                "added_at": "2026-01-01T01:00:00+08:00",
            },
        ],
    )

    result = wl.remove("2317", root=root)

    symbols = {item.ticker for item in result.items}
    assert "2317" not in symbols
    assert "2317" not in _db_symbols(root)
    assert "CLOUD1" in _db_symbols(root)
    assert "2330" in _db_symbols(root)


def test_nonempty_sqlite_wins_over_conflicting_json(root: Path) -> None:
    """非空 SQLite 為 SSOT：與 JSON 衝突時以 SQLite 為準。"""
    _write_json(
        root,
        [
            {
                "ticker": "9999",
                "name": "JSON版",
                "tags": ["json"],
                "note": "from-json",
                "added_at": "2026-01-01T00:00:00+08:00",
            }
        ],
    )
    db = StockDB.open(path=default_db_path(root))
    try:
        db.upsert_watch(
            WatchlistRow(
                symbol="2330",
                name="SQLite版",
                tags="db",
                note="from-db",
                added_at="2026-01-03T00:00:00+08:00",
            )
        )
    finally:
        db.close()

    loaded = wl.load(root)
    tickers = {item.ticker for item in loaded.items}
    assert tickers == {"2330"}
    assert loaded.items[0].note == "from-db"


def test_legacy_json_bootstraps_empty_sqlite(root: Path) -> None:
    """空 SQLite 時，legacy JSON 應 bootstrap 進 DB。"""
    _write_json(
        root,
        [
            {
                "ticker": "2454",
                "name": "聯發科",
                "tags": ["半導體"],
                "note": "bootstrap",
                "added_at": "2026-01-04T00:00:00+08:00",
            }
        ],
    )
    # 確保 DB 檔存在且 watchlist 為空
    db = StockDB.open(path=default_db_path(root))
    try:
        assert db.list_watchlist() == []
    finally:
        db.close()

    loaded = wl.load(root)

    assert {item.ticker for item in loaded.items} == {"2454"}
    assert _db_symbols(root) == {"2454"}


def test_failed_db_write_does_not_publish_json(root, monkeypatch):
    wl.add("2330", note="before", root=root)
    before = (root / "data/watchlist.json").read_bytes()
    def fail(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(wl.WatchlistRepository, "save", fail)
    with pytest.raises(RuntimeError, match="disk full"):
        wl.update("2330", note="lost", root=root)
    assert (root / "data/watchlist.json").read_bytes() == before
    assert wl.load(root).items[0].note == "before"


def test_unchanged_stale_rows_do_not_overwrite_newer_edits(root):
    wl.add("2330", root=root)
    wl.add("2454", root=root)
    stale = wl.load(root)
    wl.update("2330", note="newer", root=root)
    next(i for i in stale.items if i.ticker == "2454").note = "other"
    wl.save(stale, root)
    assert {i.ticker: i.note for i in wl.load(root).items} == {"2330": "newer", "2454": "other"}


def test_conflicting_batch_rolls_back_all_edits(root):
    wl.add("2330", root=root)
    wl.add("2454", root=root)
    stale = wl.load(root)
    stale.items.sort(key=lambda item: item.ticker, reverse=True)
    wl.update("2330", note="newer", root=root)
    for item in stale.items:
        item.note = "stale"
    with pytest.raises(wl.WatchlistConflictError):
        wl.save(stale, root)
    assert {i.ticker: i.note for i in wl.load(root).items} == {"2330": "newer", "2454": ""}


def test_empty_initialized_db_never_reimports_stale_json(root):
    assert wl.load(root).items == []
    _write_json(root, [{"ticker": "STALE"}])
    assert wl.load(root).items == []


def test_deleted_last_item_cannot_be_restored_by_stale_editor_or_json(root):
    wl.add("2330", root=root)
    stale = wl.load(root)
    wl.remove("2330", root=root)
    _write_json(root, [{"ticker": "2330"}])
    assert wl.load(root).items == []
    stale.items[0].note = "late edit"
    with pytest.raises(wl.WatchlistConflictError):
        wl.save(stale, root)
    assert wl.load(root).items == []
    # Explicit re-add records the tombstone it observed.
    wl.add("2330", root=root)
    db = StockDB.open(path=default_db_path(root))
    try:
        assert db.get_watch("2330").restored_from
    finally:
        db.close()


def test_snapshot_failure_reports_durable_success(root, monkeypatch):
    wl.add("2330", root=root)
    before = (root / "data/watchlist.json").read_bytes()
    def fail(*args, **kwargs):
        raise OSError("projection failed")
    monkeypatch.setattr(wl.os, "replace", fail)
    with pytest.raises(wl.WatchlistSnapshotError) as exc:
        wl.update("2330", note="committed", root=root)
    assert exc.value.committed
    assert wl.load(root).items[0].note == "committed"
    assert (root / "data/watchlist.json").read_bytes() == before
    assert not list((root / "data").glob(".watchlist-*.tmp"))


def test_loaded_empty_snapshot_does_not_overwrite_concurrent_add(root):
    stale = wl.load(root)
    wl.add("2330", note="other editor", root=root)
    stale.items.append(wl.WatchItem("2330"))
    with pytest.raises(wl.WatchlistConflictError):
        wl.save(stale, root)

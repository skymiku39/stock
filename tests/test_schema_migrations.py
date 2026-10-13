import sqlite3

import pytest

from bot.stock_db import StockDB


def test_legacy_database_upgrade_preserves_rows_and_receipts(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE watchlist (symbol TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT INTO watchlist(symbol, note) VALUES ('2330', 'keep')")
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT, checksum TEXT, applied_at TEXT)")
        conn.execute("INSERT INTO schema_migrations VALUES (1,'historical','original-checksum','old-time')")
    db = StockDB.open(path=path)
    assert db.get_watch("2330").note == "keep"
    assert db.latest_schema_version() == 6
    assert dict(db.conn.execute("SELECT * FROM schema_migrations WHERE version=1").fetchone())["checksum"] == "original-checksum"
    receipt = db.schema_migrations()
    db.close()
    reopened = StockDB.open(path=path)
    assert reopened.schema_migrations() == receipt
    reopened.close()


def test_migration_checksum_mismatch_does_not_rewrite_history(tmp_path):
    path = tmp_path / "bad.db"
    db = StockDB.open(path=path)
    db.conn.execute("UPDATE schema_migrations SET checksum='altered' WHERE version=6")
    db.close()
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        StockDB.open(path=path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT checksum FROM schema_migrations WHERE version=6").fetchone()[0] == "altered"


def test_failed_migration_rolls_back_ddl_and_receipt(tmp_path, monkeypatch):
    from bot import schema_migrations
    path = tmp_path / "rollback.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE watchlist (symbol TEXT PRIMARY KEY)")
    monkeypatch.setattr(schema_migrations, "MIGRATIONS", (
        (6, "broken", ("ALTER TABLE watchlist ADD COLUMN probe TEXT", "INVALID SQL")),
    ))
    with pytest.raises(sqlite3.OperationalError):
        StockDB.open(path=path)
    with sqlite3.connect(path) as conn:
        assert [r[1] for r in conn.execute("PRAGMA table_info(watchlist)")] == ["symbol"]
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()

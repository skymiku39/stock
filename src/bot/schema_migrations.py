"""Immutable, transactional migrations after the legacy compatibility baseline."""

from __future__ import annotations

import hashlib
import sqlite3

from bot.utils import now_tw


# Append a new version when changing the schema; never edit an applied entry.
MIGRATIONS = (
    (6, "watchlist-revisions-and-tombstones", (
        "ALTER TABLE watchlist ADD COLUMN revision TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE watchlist ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE watchlist ADD COLUMN restored_from TEXT NOT NULL DEFAULT ''",
        "CREATE TABLE watchlist_state (id INTEGER PRIMARY KEY CHECK(id = 1), initialized_at TEXT NOT NULL)",
    )),
    (7, "memory-status-update-preserves-search-index", (
        "DROP TRIGGER IF EXISTS memory_items_au",
    )),
    (8, "quarterly-unreported-values-remain-null", (
        """CREATE TABLE quarterly_report_nullable (
            symbol TEXT NOT NULL, period TEXT NOT NULL,
            eps REAL, revenue REAL, gross_margin REAL, op_margin REAL, net_margin REAL,
            note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '', source_date TEXT NOT NULL DEFAULT '',
            retrieved_at TEXT NOT NULL DEFAULT '', quality_status TEXT NOT NULL DEFAULT 'unknown',
            source_record_id TEXT NOT NULL DEFAULT '', PRIMARY KEY (symbol, period))""",
        "INSERT INTO quarterly_report_nullable SELECT symbol,period,eps,revenue,gross_margin,op_margin,net_margin,note,updated_at,source_url,source_date,retrieved_at,quality_status,source_record_id FROM quarterly_report",
        "DROP TABLE quarterly_report",
        "ALTER TABLE quarterly_report_nullable RENAME TO quarterly_report",
    )),
    (9, "unreported-monthly-growth-remains-null", (
        """CREATE TABLE monthly_revenue_nullable (
            symbol TEXT NOT NULL, year_month TEXT NOT NULL, revenue REAL NOT NULL DEFAULT 0,
            yoy_pct REAL, mom_pct REAL, cumulative REAL NOT NULL DEFAULT 0, cum_yoy_pct REAL,
            note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '', source_date TEXT NOT NULL DEFAULT '',
            retrieved_at TEXT NOT NULL DEFAULT '', quality_status TEXT NOT NULL DEFAULT 'unknown',
            source_record_id TEXT NOT NULL DEFAULT '', PRIMARY KEY (symbol, year_month))""",
        "INSERT INTO monthly_revenue_nullable SELECT symbol,year_month,revenue,yoy_pct,mom_pct,cumulative,cum_yoy_pct,note,updated_at,source_url,source_date,retrieved_at,quality_status,source_record_id FROM monthly_revenue",
        "DROP TABLE monthly_revenue",
        "ALTER TABLE monthly_revenue_nullable RENAME TO monthly_revenue",
    )),
    (10, "intraday-preserve-tick-sequence-and-quotes", (
        """CREATE TABLE intraday_bars_sequenced (
            symbol TEXT NOT NULL, ts TEXT NOT NULL, interval TEXT NOT NULL DEFAULT '1m',
            open REAL NOT NULL DEFAULT 0, high REAL NOT NULL DEFAULT 0,
            low REAL NOT NULL DEFAULT 0, close REAL NOT NULL DEFAULT 0,
            volume REAL NOT NULL DEFAULT 0, amount REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'shioaji', updated_at TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '', source_date TEXT NOT NULL DEFAULT '',
            retrieved_at TEXT NOT NULL DEFAULT '', quality_status TEXT NOT NULL DEFAULT 'unknown',
            source_record_id TEXT NOT NULL DEFAULT '', sequence INTEGER NOT NULL DEFAULT 0,
            bid_price REAL, bid_volume REAL, ask_price REAL, ask_volume REAL, tick_type INTEGER,
            PRIMARY KEY(symbol,ts,interval,sequence))""",
        "INSERT INTO intraday_bars_sequenced (symbol,ts,interval,open,high,low,close,volume,amount,source,updated_at,source_url,source_date,retrieved_at,quality_status,source_record_id) SELECT symbol,ts,interval,open,high,low,close,volume,amount,source,updated_at,source_url,source_date,retrieved_at,quality_status,source_record_id FROM intraday_bars",
        "DROP TABLE intraday_bars",
        "ALTER TABLE intraday_bars_sequenced RENAME TO intraday_bars",
    )),
)


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Run inside the caller's transaction so DDL and its receipt commit together."""
    for version, name, statements in MIGRATIONS:
        checksum = hashlib.sha256(
            f"{version}:{name}:".encode() + "\n".join(statements).encode()
        ).hexdigest()
        existing = conn.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = ?", (version,),
        ).fetchone()
        if existing:
            if existing["name"] != name or existing["checksum"] != checksum:
                raise RuntimeError(f"Migration {version} checksum mismatch; database unchanged")
            continue
        for sql in statements:
            conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_migrations VALUES (?,?,?,?)",
            (version, name, checksum, now_tw().isoformat()),
        )

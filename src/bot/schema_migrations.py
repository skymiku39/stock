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

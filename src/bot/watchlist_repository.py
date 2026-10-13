"""SQLite boundary for watchlist edits, optimistic concurrency and deletion."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Iterable
from uuid import uuid4

from bot.utils import now_tw

if TYPE_CHECKING:
    from bot.stock_db import StockDB


class WatchlistConflictError(RuntimeError):
    """An editor tried to save a version that has changed since it was loaded."""


def version_token(row: dict[str, Any] | None) -> str:
    # Include contents: manual Sheets edits may retain the old revision cell.
    if row is None:
        return ""
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def choose_watchlist_row(left: dict, right: dict) -> dict | None:
    """Deletion wins unless an explicit re-add observed that exact tombstone."""
    if bool(left.get("deleted_at")) == bool(right.get("deleted_at")):
        return None  # Ordinary update timestamp comparison.
    deleted, active = (left, right) if left.get("deleted_at") else (right, left)
    if deleted.get("revision") and active.get("restored_from") == deleted["revision"]:
        return active
    return deleted


class WatchlistRepository:
    def __init__(self, db: StockDB):
        self.db = db

    def initialized(self) -> bool:
        return self.db.conn.execute("SELECT 1 FROM watchlist_state WHERE id=1").fetchone() is not None

    @staticmethod
    def mark_initialized(conn) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist_state VALUES (1,?)", (now_tw().isoformat(),),
        )

    def rows(self, *, include_deleted: bool = False) -> list[dict]:
        condition = "" if include_deleted else "WHERE deleted_at = ''"
        return [dict(row) for row in self.db.conn.execute(
            f"SELECT * FROM watchlist {condition} ORDER BY added_at DESC, symbol",
        ).fetchall()]

    def get(self, symbol: str) -> dict | None:
        row = self.db.conn.execute("SELECT * FROM watchlist WHERE symbol=?", (symbol,)).fetchone()
        return dict(row) if row else None

    def initialize(self, legacy_rows: Iterable[dict]) -> None:
        with self.db.transaction() as conn:
            if self.initialized():
                return
            if not conn.execute("SELECT 1 FROM watchlist LIMIT 1").fetchone():
                for row in legacy_rows:
                    self._write(conn, row)
            self.mark_initialized(conn)

    @staticmethod
    def _write(conn, row: dict, *, current: dict | None = None, deleted: bool = False) -> None:
        now = now_tw().isoformat(timespec="microseconds")
        current = current or {}
        restored_from = current.get("restored_from", "")
        if current.get("deleted_at") and not deleted:
            restored_from = current.get("revision", "")
        values = {
            "symbol": row["symbol"], "name": row.get("name", ""),
            "tags": row.get("tags", ""), "note": row.get("note", ""),
            "added_at": current.get("added_at") or row.get("added_at") or now,
            "updated_at": now, "revision": uuid4().hex,
            "deleted_at": now if deleted else "", "restored_from": restored_from,
        }
        conn.execute(
            f"INSERT OR REPLACE INTO watchlist ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )

    def save(self, changes: Iterable[tuple[dict, str]]) -> None:
        """All edits succeed together; expected token '' means the row must be absent."""
        with self.db.transaction() as conn:
            for row, expected in changes:
                current = self.get(row["symbol"])
                if version_token(current) != expected:
                    raise WatchlistConflictError(f"{row['symbol']} 已被更新或刪除，請重新載入後再儲存")
                self._write(conn, row, current=current)
            self.mark_initialized(conn)

    def upsert(self, row: dict, *, expected: str | None = None) -> None:
        """Compatibility DAO: supplied expected token enables optimistic editing."""
        with self.db.transaction() as conn:
            current = self.get(row["symbol"])
            if expected is not None and version_token(current) != expected:
                raise WatchlistConflictError(f"{row['symbol']} 已被更新或刪除，請重新載入")
            if current and not row.get("name"):
                row = {**row, "name": current["name"]}
            self._write(conn, row, current=current)
            self.mark_initialized(conn)

    def remove(self, symbol: str, *, expected: str | None = None) -> None:
        with self.db.transaction() as conn:
            current = self.get(symbol)
            if expected is not None and version_token(current) != expected:
                raise WatchlistConflictError(f"{symbol} 已被更新，請重新載入後再刪除")
            if current and not current.get("deleted_at"):
                self._write(conn, current, current=current, deleted=True)
            self.mark_initialized(conn)

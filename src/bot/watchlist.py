"""Watchlist application API. SQLite commits precede derived JSON snapshots.

JSON is imported once, only before initialization. Explicit deletions remain as
SQLite tombstones and missing items in an editor snapshot never imply deletion.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.stock_db import StockDB, default_db_path
from bot.watchlist_repository import WatchlistConflictError, WatchlistRepository, version_token


@dataclass
class WatchItem:
    ticker: str
    name: str = ""
    tags: List[str] = field(default_factory=list)
    note: str = ""
    added_at: str = ""
    revision: str = ""


@dataclass
class WatchList:
    items: List[WatchItem] = field(default_factory=list)
    _baseline: dict = field(default_factory=dict, repr=False, compare=False)


class WatchlistSnapshotError(RuntimeError):
    """The durable commit succeeded but refreshing its JSON projection failed."""
    committed = True


def _path(root: Optional[Path] = None) -> Path:
    return (root or Path.cwd()) / "data" / "watchlist.json"


def _item_row(item: WatchItem) -> dict:
    return dict(symbol=item.ticker, name=item.name, tags=",".join(item.tags),
                note=item.note, added_at=item.added_at)


def _load_json(root: Optional[Path] = None) -> WatchList:
    p = _path(root)
    if not p.exists():
        return WatchList()
    # Malformed legacy data must not be silently accepted as an empty list.
    raw = json.loads(p.read_text(encoding="utf-8"))
    return WatchList(items=[WatchItem(
        ticker=str(r.get("ticker", "")).strip(), name=str(r.get("name", "")),
        tags=list(r.get("tags", []) or []), note=str(r.get("note", "")),
        added_at=str(r.get("added_at", "")),
    ) for r in raw.get("items", []) if str(r.get("ticker", "")).strip()])


def _write_json(wl: WatchList, root: Optional[Path] = None) -> Path:
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent,
                                         prefix=".watchlist-", suffix=".tmp", delete=False) as fp:
            temporary = Path(fp.name)
            json.dump({"items": [asdict(i) for i in wl.items]}, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temporary, p)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    mirror_file_to_cloud(p, root=root)
    return p


def _read(repo: WatchlistRepository) -> WatchList:
    result = WatchList(items=[WatchItem(
        ticker=r["symbol"], name=r["name"], tags=[t.strip() for t in r["tags"].split(",") if t.strip()],
        note=r["note"], added_at=r["added_at"], revision=version_token(r),
    ) for r in repo.rows()])
    result._baseline = {i.ticker: asdict(i) for i in result.items}
    return result


def _open(root: Optional[Path]) -> tuple[StockDB, WatchlistRepository]:
    db = StockDB.open(path=default_db_path(root))
    repo = WatchlistRepository(db)
    try:
        if not repo.initialized():
            # Existing SQLite rows always take precedence over legacy JSON.
            legacy = WatchList()
            if not repo.rows(include_deleted=True):
                restore_file_from_cloud(_path(root), root=root)
                legacy = _load_json(root)
            repo.initialize(_item_row(i) for i in legacy.items)
        return db, repo
    except Exception:
        db.close()
        raise


def _project(repo: WatchlistRepository, root: Optional[Path]) -> WatchList:
    result = _read(repo)
    try:
        _write_json(result, root)
    except Exception as exc:
        raise WatchlistSnapshotError("SQLite 已儲存；JSON 快照更新失敗，請重新載入清單") from exc
    return result


def load(root: Optional[Path] = None) -> WatchList:
    db, repo = _open(root)
    try:
        return _read(repo)
    finally:
        db.close()


def save(wl: WatchList, root: Optional[Path] = None) -> Path:
    """Atomically save edited rows, checking the versions loaded by the editor."""
    db, repo = _open(root)
    try:
        changes = []
        seen = set()
        for item in wl.items:
            if not item.ticker or item.ticker in seen:
                raise ValueError("watchlist ticker 必須非空且不可重複")
            seen.add(item.ticker)
            original = wl._baseline.get(item.ticker)
            if original is None or asdict(item) != original:
                expected = original["revision"] if original is not None else item.revision
                changes.append((_item_row(item), expected))
        repo.save(changes)
        current = _project(repo, root)
        wl.items, wl._baseline = current.items, current._baseline
        return _path(root)
    finally:
        db.close()


def add(ticker: str, *, name: str = "", tags: Optional[List[str]] = None,
        note: str = "", root: Optional[Path] = None) -> WatchList:
    ticker = ticker.strip()
    if not ticker:
        return load(root)
    db, repo = _open(root)
    try:
        current = repo.get(ticker)
        active = current if current and not current.get("deleted_at") else {}
        row = dict(symbol=ticker, name=active.get("name") or name,
                   tags=",".join(sorted(set(filter(None, active.get("tags", "").split(","))) | set(tags or []))),
                   note=active.get("note") or note, added_at=active.get("added_at", ""))
        repo.save([(row, version_token(current))])
        return _project(repo, root)
    finally:
        db.close()


def remove(ticker: str, root: Optional[Path] = None, *, expected_revision: str | None = None) -> WatchList:
    db, repo = _open(root)
    try:
        if ticker.strip():
            current = repo.get(ticker.strip())
            repo.remove(ticker.strip(), expected=expected_revision if expected_revision is not None else version_token(current))
        return _project(repo, root)
    finally:
        db.close()


def update(ticker: str, *, name: Optional[str] = None, tags: Optional[List[str]] = None,
           note: Optional[str] = None, root: Optional[Path] = None) -> WatchList:
    wl = load(root)
    for item in wl.items:
        if item.ticker == ticker:
            if name is not None:
                item.name = name
            if tags is not None:
                item.tags = sorted(set(tags))
            if note is not None:
                item.note = note
            break
    save(wl, root)
    return wl


def merge_etf_focus(
    *,
    min_consensus: int = 2,
    tag: str = "ETF共識",
    root: Optional[Path] = None,
) -> WatchList:
    """把目前 ETF 共識焦點 (持有檔數 >= min_consensus) 全部加入 watchlist。"""
    from bot.active_etf import list_holdings_dates, load_active_etfs, load_holdings
    from bot.etf_consensus import build_consensus

    etfs = load_active_etfs(root)
    etf_meta = {e.symbol: e for e in etfs}
    holds = {}
    for e in etfs:
        dates = list_holdings_dates(e.symbol, root)
        if not dates:
            continue
        h = load_holdings(e.symbol, dates[0], root)
        if h:
            holds[e.symbol] = h
    consensus = build_consensus(holds, etf_meta, min_etf_count=min_consensus)
    for c in consensus:
        add(c.ticker, name=c.name, tags=[tag], root=root)
    return load(root)


def merge_pipeline_focus(
    run_id: Optional[str] = None,
    *,
    tag: str = "管線焦點",
    root: Optional[Path] = None,
) -> WatchList:
    """把最近一次 (或指定) pipeline run 的焦點個股加進來。"""
    from bot.data_pipeline import list_pipeline_runs, load_pipeline_run

    if run_id is None:
        runs = list_pipeline_runs(root or Path.cwd())
        if not runs:
            return load(root)
        run_id = runs[0]["run_id"]
    run = load_pipeline_run(root or Path.cwd(), run_id)
    if run is None:
        return load(root)
    for t in run.focus_tickers:
        add(t, tags=[tag], root=root)
    return load(root)


__all__ = [
    "WatchItem",
    "WatchList",
    "WatchlistConflictError",
    "WatchlistSnapshotError",
    "add",
    "load",
    "merge_etf_focus",
    "merge_pipeline_focus",
    "remove",
    "save",
    "update",
]

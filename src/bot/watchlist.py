"""watchlist -- 使用者自訂的個股監控清單。

設計
====
* **雙寫 (Dual-write)**：每次新增/修改/刪除同時更新
    1. JSON 檔 (`data/watchlist.json`) — 向後相容、易閱讀、可手編
    2. SQLite (`stock_db.watchlist` table) — 支援雲端同步、複雜查詢
* 每檔含 ticker / name / tags / note / added_at
* 提供「合併 ETF 共識焦點」與「合併最近一次 pipeline 焦點」的便利方法

> 若 SQLite 寫入失敗，JSON 依然會成功；watchlist 永遠至少有一份備援。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.utils import get_logger, mk_folder, now_tw


@dataclass
class WatchItem:
    ticker: str
    name: str = ""
    tags: List[str] = field(default_factory=list)
    note: str = ""
    added_at: str = ""


@dataclass
class WatchList:
    items: List[WatchItem] = field(default_factory=list)


def _path(root: Optional[Path] = None) -> Path:
    return (root or Path.cwd()) / "data" / "watchlist.json"


def load(root: Optional[Path] = None) -> WatchList:
    p = _path(root)
    if not p.exists():
        return WatchList()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        items = []
        for r in raw.get("items", []):
            items.append(WatchItem(
                ticker=str(r.get("ticker", "")).strip(),
                name=str(r.get("name", "")),
                tags=list(r.get("tags", []) or []),
                note=str(r.get("note", "")),
                added_at=str(r.get("added_at", "")),
            ))
        return WatchList(items=[i for i in items if i.ticker])
    except Exception:
        get_logger("watchlist").exception("讀取 watchlist 失敗")
        return WatchList()


def save(wl: WatchList, root: Optional[Path] = None) -> Path:
    p = _path(root)
    mk_folder(str(p.parent))
    p.write_text(
        json.dumps(
            {"items": [asdict(i) for i in wl.items]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    _sync_to_db(wl, root)
    return p


def _sync_to_db(wl: WatchList, root: Optional[Path] = None) -> None:
    """JSON 寫入後同步到 SQLite (失敗不影響主流程)。"""
    try:
        from bot.stock_db import StockDB, WatchlistRow, default_db_path

        db_path = default_db_path(root)
        db = StockDB.open(path=db_path)

        existing = {w.symbol for w in db.list_watchlist()}
        keep: set[str] = set()
        for it in wl.items:
            keep.add(it.ticker)
            db.upsert_watch(WatchlistRow(
                symbol=it.ticker,
                name=it.name,
                tags=",".join(it.tags),
                note=it.note,
                added_at=it.added_at,
            ))
        for sym in existing - keep:
            db.remove_watch(sym)
    except Exception:
        get_logger("watchlist").exception("watchlist 同步到 SQLite 失敗 (忽略)")


def add(
    ticker: str,
    *,
    name: str = "",
    tags: Optional[List[str]] = None,
    note: str = "",
    root: Optional[Path] = None,
) -> WatchList:
    ticker = ticker.strip()
    if not ticker:
        return load(root)
    wl = load(root)
    for it in wl.items:
        if it.ticker == ticker:
            if name and not it.name:
                it.name = name
            if tags:
                it.tags = sorted(set((it.tags or []) + list(tags)))
            if note and not it.note:
                it.note = note
            save(wl, root)
            return wl
    wl.items.append(WatchItem(
        ticker=ticker, name=name,
        tags=sorted(set(tags or [])),
        note=note,
        added_at=now_tw().isoformat(timespec="seconds"),
    ))
    save(wl, root)
    return wl


def remove(ticker: str, root: Optional[Path] = None) -> WatchList:
    wl = load(root)
    wl.items = [i for i in wl.items if i.ticker != ticker]
    save(wl, root)
    return wl


def update(
    ticker: str,
    *,
    name: Optional[str] = None,
    tags: Optional[List[str]] = None,
    note: Optional[str] = None,
    root: Optional[Path] = None,
) -> WatchList:
    wl = load(root)
    for it in wl.items:
        if it.ticker == ticker:
            if name is not None:
                it.name = name
            if tags is not None:
                it.tags = sorted(set(tags))
            if note is not None:
                it.note = note
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
    "add",
    "load",
    "merge_etf_focus",
    "merge_pipeline_focus",
    "remove",
    "save",
    "update",
]

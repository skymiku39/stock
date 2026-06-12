"""從近期 LLM 報告彙整回測/抓取用的股票清單。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from bot.intraday_live import extract_llm_mentions
from bot.intraday_pipeline import load_intraday_by_date
from bot.next_day_watch_pipeline import load_next_day_by_date
from bot.stock_db import StockDB, default_db_path
from bot.utils import now_tw


def _add_unique(order: List[str], seen: set[str], ticker: str) -> None:
    t = str(ticker or "").strip()
    if not t or not t.isdigit() or t in seen:
        return
    seen.add(t)
    order.append(t)


def collect_llm_symbols(
    root: Optional[Path] = None,
    *,
    lookback_days: int = 5,
    max_per_report: int = 25,
    include_intraday: bool = True,
    include_next_day: bool = True,
    db: Optional[StockDB] = None,
) -> List[str]:
    """合併最近幾次 LLM 報告 rankings 內的所有個股（去重、保序）。"""
    root = root or Path.cwd()
    database = db or StockDB.open(path=default_db_path(root))
    today = now_tw().date()
    order: List[str] = []
    seen: set[str] = set()

    for offset in range(lookback_days + 1):
        day = today - dt.timedelta(days=offset)
        day_s = day.isoformat()

        if include_intraday:
            row = database.get_llm_daily_report("intraday", day_s, mode="")
            if row and row.payload_json:
                payload = json.loads(row.payload_json)
                for item in (payload.get("rankings") or [])[:max_per_report]:
                    if isinstance(item, dict):
                        _add_unique(order, seen, str(item.get("ticker") or ""))
            report = load_intraday_by_date(root, day)
            if report:
                for m in extract_llm_mentions(report, max_tickers=max_per_report):
                    _add_unique(order, seen, m["ticker"])

        if include_next_day:
            for mode in ("update", "draft"):
                row = database.get_llm_daily_report(
                    "next_day_watch", day_s, mode=mode,
                )
                if row and row.payload_json:
                    payload = json.loads(row.payload_json)
                    for item in (payload.get("rankings") or [])[:max_per_report]:
                        if isinstance(item, dict):
                            _add_unique(order, seen, str(item.get("ticker") or ""))
            nd = load_next_day_by_date(root, day, prefer_update=True)
            if nd:
                for m in extract_llm_mentions(nd, max_tickers=max_per_report):
                    _add_unique(order, seen, m["ticker"])

    return order


def llm_symbol_metadata(
    root: Optional[Path] = None,
    symbols: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """回傳各股在最近 intraday 報告中的排名/分數（若有）。"""
    root = root or Path.cwd()
    database = StockDB.open(path=default_db_path(root))
    today = now_tw().date().isoformat()
    meta: Dict[str, Dict[str, Any]] = {}
    row = database.get_llm_daily_report("intraday", today, mode="")
    if not row or not row.payload_json:
        return meta
    payload = json.loads(row.payload_json)
    for idx, item in enumerate(payload.get("rankings") or [], 1):
        if not isinstance(item, dict):
            continue
        t = str(item.get("ticker") or "").strip()
        if symbols and t not in symbols:
            continue
        meta[t] = {
            "rank": idx,
            "name": item.get("name", ""),
            "day_trade_score": item.get("day_trade_score"),
            "action": item.get("action", ""),
        }
    return meta

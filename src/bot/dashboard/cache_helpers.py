"""Streamlit cache wrappers for dashboard read-mostly data."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

from bot.dashboard.common import PROJECT_ROOT
from bot.env_io import load_env


@st.cache_data(ttl=120, show_spinner=False)
def cached_env_values() -> Dict[str, str]:
    return load_env()


@st.cache_data(ttl=60, show_spinner=False)
def cached_llm_today_stats(_project_root: str) -> Tuple[int, int, int]:
    """今日 LLM 呼叫筆數與 token 統計 (上限 500 筆，避免每次換頁掃 1 萬行)。"""
    try:
        from bot.llm_log import get_call_logger

        today_log = get_call_logger().read(dt.date.today(), limit=500)
        tokens_in = sum(r.tokens_in or 0 for r in today_log)
        tokens_out = sum(r.tokens_out or 0 for r in today_log)
        return len(today_log), tokens_in, tokens_out
    except Exception:
        return 0, 0, 0


@st.cache_data(ttl=300, show_spinner=False)
def cached_board_rows_summary(
    symbols_key: str,
    _db_mtime: float,
    _project_root: str,
) -> pd.DataFrame:
    """K 線看板摘要表快取 (symbols 變更或 DB 更新時失效)。"""
    from bot.dashboard import pages as dash_pages
    from bot.stock_db import StockDB, default_db_path

    symbols = [s for s in symbols_key.split(",") if s]
    if not symbols:
        return pd.DataFrame()
    db = StockDB.open(path=default_db_path(Path(_project_root)))
    return dash_pages._board_rows_summary(symbols, db)


@st.cache_data(ttl=60, show_spinner=False)
def cached_price_band_heat(
    price_low: float,
    price_high: float,
    limit: int,
    exclude_dr: bool,
    _project_root: str,
) -> Dict[str, Any]:
    from bot.env_io import load_env
    from bot.price_band_heat import DEFAULT_WATCH_SYMBOLS, fetch_price_band_heat

    env = load_env()
    symbols_raw = env.get("SYMBOLS", "")
    watch = [s.strip() for s in symbols_raw.split(",") if s.strip()] or list(DEFAULT_WATCH_SYMBOLS)
    result = fetch_price_band_heat(
        root=Path(_project_root),
        price_low=price_low,
        price_high=price_high,
        limit=limit,
        exclude_dr=exclude_dr,
        watch_symbols=watch,
    )
    return result.to_dict()


@st.cache_data(ttl=20, show_spinner=False)
def cached_hot_stock_futures(
    limit: int,
    direction: str,
    _project_root: str,
) -> Dict[str, Any]:
    from bot.hot_stock_futures import fetch_hot_stock_futures

    result = fetch_hot_stock_futures(
        limit=limit,
        direction=direction,  # type: ignore[arg-type]
    )
    return result.to_dict()


@st.cache_data(ttl=30, show_spinner=False)
def cached_market_movers(
    limit: int,
    direction: str,
    exclude_etf: bool,
    _project_root: str,
) -> Dict[str, Any]:
    from bot.market_movers import fetch_market_movers

    result = fetch_market_movers(
        root=Path(_project_root),
        limit=limit,
        direction=direction,  # type: ignore[arg-type]
        exclude_etf=exclude_etf,
    )
    return result.to_dict()


@st.cache_data(ttl=20, show_spinner=False)
def cached_intraday_tracking(
    report_date: str,
    tickers_key: str,
    max_tickers: int,
    refresh_technicals: bool,
    refresh_chips: bool,
    _project_root: str,
) -> Dict[str, Any]:
    """當沖即時追蹤表 (短 TTL，避免每 30s 整頁重打 MIS)。"""
    from bot.intraday_live import build_live_tracking_rows
    from bot.intraday_pipeline import load_intraday_by_date

    report = load_intraday_by_date(Path(_project_root), report_date)
    if not report:
        return {"rows": [], "asof": "", "error": "no_report"}
    return build_live_tracking_rows(
        report,
        root=Path(_project_root),
        max_tickers=max_tickers,
        refresh_quotes=True,
        refresh_technicals=refresh_technicals,
        refresh_chips=refresh_chips,
        refresh_news=False,
        include_news=False,
    )

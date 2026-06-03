"""market_meta -- 個股市場別判斷 (上市 TWSE / 上櫃 TPEx / ETF)。

許多資料來源端點分「上市」與「上櫃」兩套；本模組提供一個共用的判斷工具，
讓 technicals / fundamentals / chips 等模組能正確路由到對應的資料來源。

設計
====
* 以「全市場代號清單」建立 {代號: 市場別} 對照，快取於 data/meta/market_map.json (TTL 1 天)。
  - 上市代號：TWSE STOCK_DAY_AVG_ALL
  - 上櫃代號：TPEx 上櫃每日收盤行情 OpenAPI
* ETF 判斷採代號規則 (台股 ETF 代號以 "00" 開頭)，與上市/上櫃正交。
* 任何網路失敗都 graceful 退化為 "unknown"，呼叫端可自行決定預設行為。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

import requests

from bot.cloud_file_cache import restore_file_from_cloud, write_json_cache
from bot.utils import get_logger, mk_folder, now_tw

# 市場別常數
TWSE = "twse"   # 上市
TPEX = "tpex"   # 上櫃
UNKNOWN = "unknown"

URL_TWSE_CODES = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_AVG_ALL"
URL_TPEX_CODES = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

# 程序內快取，避免同一次執行重複讀檔/打網路
_MARKET_MAP: Optional[Dict[str, str]] = None


def _map_path(root: Optional[Path]) -> Path:
    base = (root or Path.cwd()) / "data" / "meta"
    mk_folder(str(base))
    return base / "market_map.json"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Referer": "https://www.tpex.org.tw/",
    })
    return s


def _build_market_map(
    *,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, str]:
    """從 TWSE / TPEx 全市場清單建立 {代號: 市場別}。"""
    log = logger or get_logger("market-meta")
    sess = session or _session()
    out: Dict[str, str] = {}
    # 上市
    try:
        r = sess.get(URL_TWSE_CODES, timeout=20)
        if r.status_code == 200:
            for row in r.json() or []:
                code = str(row.get("Code") or "").strip()
                if code:
                    out[code] = TWSE
    except Exception:
        log.exception("讀取上市代號清單失敗")
    # 上櫃 (不覆蓋已存在的上市；理論上代號不重疊)
    try:
        r = sess.get(URL_TPEX_CODES, timeout=30)
        if r.status_code == 200:
            for row in r.json() or []:
                code = str(row.get("SecuritiesCompanyCode") or "").strip()
                if code and code not in out:
                    out[code] = TPEX
    except Exception:
        log.exception("讀取上櫃代號清單失敗")
    return out


def load_market_map(
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    force_refresh: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, str]:
    """載入 (或建立) 市場別對照表，每日快取一次。"""
    global _MARKET_MAP
    if _MARKET_MAP is not None and not force_refresh:
        return _MARKET_MAP

    path = _map_path(root)
    restore_file_from_cloud(path, root=root)
    today = now_tw().date().isoformat()
    if not force_refresh and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("date") == today and cached.get("map"):
                _MARKET_MAP = {str(k): str(v) for k, v in cached["map"].items()}
                return _MARKET_MAP
        except Exception:
            pass

    mp = _build_market_map(session=session, logger=logger)
    if mp:
        try:
            write_json_cache(path, {"date": today, "map": mp}, root=root)
        except Exception:
            pass
        _MARKET_MAP = mp
        return mp

    # 建表失敗：盡量用舊快取，否則回空
    restore_file_from_cloud(path, root=root)
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            _MARKET_MAP = {str(k): str(v) for k, v in (cached.get("map") or {}).items()}
            return _MARKET_MAP
        except Exception:
            pass
    _MARKET_MAP = {}
    return _MARKET_MAP


def detect_market(
    ticker: str,
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> str:
    """回傳個股市場別：'twse'(上市) / 'tpex'(上櫃) / 'unknown'。"""
    ticker = str(ticker).strip()
    if not ticker:
        return UNKNOWN
    mp = load_market_map(root=root, session=session, logger=logger)
    return mp.get(ticker, UNKNOWN)


def is_etf(ticker: str) -> bool:
    """台股 ETF / ETN 代號慣例以 '00' 開頭 (上市與上櫃皆然)。"""
    t = str(ticker).strip()
    return len(t) >= 4 and t.startswith("00")


def market_label(market: str) -> str:
    return {TWSE: "上市", TPEX: "上櫃"}.get(market, "未知市場")


__all__ = [
    "TPEX",
    "TWSE",
    "UNKNOWN",
    "detect_market",
    "is_etf",
    "load_market_map",
    "market_label",
]

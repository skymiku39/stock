"""hot_stock_futures -- 熱門個股期貨行情 (TAIFEX MIS getQuoteList).

資料來源
========
`https://mis.taifex.com.tw/futures/api/getQuoteList`

對應網頁：
`https://mis.taifex.com.tw/futures/RegularSession/StockProducts/HotStockFutures/`
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional

import requests

from bot.utils import get_logger, now_tw

TAIFEX_MIS_API_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
TAIFEX_MIS_PAGE_URL = (
    "https://mis.taifex.com.tw/futures/RegularSession/StockProducts/HotStockFutures/"
)

HOT_STOCK_FUTURES_PAGE_ATTR: Dict[str, str] = {
    "MarketType": "0",
    "SymbolType": "F",
    "KindID": "4",
    "Hot": "T",
    "CID": "",
    "ExpireMonth": "",
    "SortColumn": "CTotalVolume",
    "AscDesc": "D",
}

SortDirection = Literal["volume", "gainers", "losers", "abs"]


@dataclass
class HotStockFuturesRow:
    symbol_id: str
    spot_id: str = ""
    name: str = ""
    name_en: str = ""
    last_price: Optional[float] = None
    ref_price: Optional[float] = None
    diff: Optional[float] = None
    pct_chg: Optional[float] = None
    amp_rate: Optional[float] = None
    volume: int = 0
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    high_price: Optional[float] = None
    low_price: Optional[float] = None
    open_price: Optional[float] = None
    quote_time: str = ""
    status: str = ""


@dataclass
class HotStockFuturesResult:
    asof: str
    direction: str
    limit: int
    quote_count: int = 0
    fetched: int = 0
    duration_sec: float = 0.0
    rows: List[HotStockFuturesRow] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            **{k: v for k, v in asdict(self).items() if k != "rows"},
            "rows": [asdict(r) for r in self.rows],
        }


def _session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://mis.taifex.com.tw",
        "Referer": TAIFEX_MIS_PAGE_URL,
    })
    return sess


def _parse_float(value: Any) -> Optional[float]:
    try:
        text = str(value or "").replace(",", "").strip()
        if not text or text == "-":
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int:
    try:
        text = str(value or "").replace(",", "").strip()
        if not text or text == "-":
            return 0
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _format_quote_time(raw: str) -> str:
    text = str(raw or "").strip()
    if len(text) == 6 and text.isdigit():
        return f"{text[:2]}:{text[2:4]}:{text[4:6]}"
    return text


def _parse_quote_item(item: Dict[str, Any]) -> HotStockFuturesRow:
    last_price = _parse_float(item.get("CLastPrice"))
    ref_price = _parse_float(item.get("CRefPrice"))
    diff = _parse_float(item.get("CDiff"))
    pct_chg = _parse_float(item.get("CDiffRate"))
    if pct_chg is None and last_price is not None and ref_price and ref_price > 0:
        pct_chg = round(100 * (last_price - ref_price) / ref_price, 2)
    if diff is None and last_price is not None and ref_price is not None:
        diff = round(last_price - ref_price, 4)
    return HotStockFuturesRow(
        symbol_id=str(item.get("SymbolID") or "").strip(),
        spot_id=str(item.get("SpotID") or "").strip(),
        name=str(item.get("DispCName") or "").strip(),
        name_en=str(item.get("DispEName") or "").strip(),
        last_price=last_price,
        ref_price=ref_price,
        diff=diff,
        pct_chg=pct_chg,
        amp_rate=_parse_float(item.get("CAmpRate")),
        volume=_parse_int(item.get("CTotalVolume")),
        bid_price=_parse_float(item.get("CBidPrice1")),
        ask_price=_parse_float(item.get("CAskPrice1")),
        high_price=_parse_float(item.get("CHighPrice")),
        low_price=_parse_float(item.get("CLowPrice")),
        open_price=_parse_float(item.get("COpenPrice")),
        quote_time=_format_quote_time(str(item.get("CTime") or "")),
        status=str(item.get("Status") or "").strip(),
    )


def _sort_rows(rows: List[HotStockFuturesRow], direction: SortDirection) -> List[HotStockFuturesRow]:
    if direction == "volume":
        return sorted(rows, key=lambda r: r.volume, reverse=True)
    if direction == "gainers":
        return sorted(rows, key=lambda r: r.pct_chg if r.pct_chg is not None else float("-inf"), reverse=True)
    if direction == "losers":
        return sorted(rows, key=lambda r: r.pct_chg if r.pct_chg is not None else float("inf"))
    return sorted(rows, key=lambda r: abs(r.pct_chg or 0.0), reverse=True)


def fetch_hot_stock_futures(
    *,
    limit: int = 50,
    direction: SortDirection = "volume",
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> HotStockFuturesResult:
    """抓取 TAIFEX 熱門個股期貨行情。"""
    log = logger or get_logger("hot-stock-futures")
    t0 = time.time()
    result = HotStockFuturesResult(
        asof=now_tw().isoformat(timespec="seconds"),
        direction=direction,
        limit=max(1, limit),
    )
    sess = session or _session()
    try:
        resp = sess.post(
            TAIFEX_MIS_API_URL,
            json=dict(HOT_STOCK_FUTURES_PAGE_ATTR),
            timeout=25,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        msg = f"TAIFEX MIS getQuoteList 失敗: {exc}"
        log.warning(msg)
        result.errors.append(msg)
        result.duration_sec = round(time.time() - t0, 2)
        return result

    if str(payload.get("RtCode") or "") not in ("0", ""):
        msg = f"TAIFEX RtCode={payload.get('RtCode')} {payload.get('RtMsg', '')}"
        result.errors.append(msg.strip())

    rt_data = payload.get("RtData") or {}
    result.quote_count = _parse_int(rt_data.get("QuoteCount"))
    rows = [_parse_quote_item(item) for item in (rt_data.get("QuoteList") or [])]
    rows = [r for r in rows if r.symbol_id]
    result.fetched = len(rows)
    sorted_rows = _sort_rows(rows, direction)
    result.rows = sorted_rows[: result.limit]
    result.duration_sec = round(time.time() - t0, 2)
    log.info(
        "hot stock futures %s: quote_count=%d fetched=%d -> top %d (%.1fs)",
        direction,
        result.quote_count,
        result.fetched,
        len(result.rows),
        result.duration_sec,
    )
    return result


__all__ = [
    "HOT_STOCK_FUTURES_PAGE_ATTR",
    "HotStockFuturesResult",
    "HotStockFuturesRow",
    "SortDirection",
    "TAIFEX_MIS_API_URL",
    "TAIFEX_MIS_PAGE_URL",
    "fetch_hot_stock_futures",
]

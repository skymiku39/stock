"""富果 Fugle：台股日內報價。金鑰走標頭 X-API-KEY。"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional, Tuple

from bot.free_sources.base import BaseClient
from bot.free_sources.quota import TAIPEI

BASE_URL = "https://api.fugle.tw/marketdata/v1.0/stock"


class FugleClient(BaseClient):
    source = "fugle"

    def intraday_quote(self, symbol: str, oddlot: bool = False) -> dict:
        params = {"type": "oddlot"} if oddlot else None
        return self._get(
            f"{BASE_URL}/intraday/quote/{symbol}", params, {"X-API-KEY": self._api_key}
        )


def usable_last_trade(quote: Any, today: dt.date) -> Optional[Tuple[float, dt.datetime]]:
    """可用的當日成交價 (price, 台灣時間 datetime)，否則 None。

    用 lastTrade 而非 lastPrice（後者含試撮）；lastTrade.time 為微秒；
    isTrial 為 true（試撮）時拒絕；2026-11-24 實測盤中報價不帶 isTrial 欄位，
    缺欄位視為非試撮。未暫停交易。
    """
    if not isinstance(quote, dict) or quote.get("isTrial") is True:
        return None
    halt = quote.get("tradingHalt")
    if isinstance(halt, dict) and halt.get("isHalted"):
        return None
    trade = quote.get("lastTrade")
    if not isinstance(trade, dict):
        return None
    price, raw_time = trade.get("price"), trade.get("time")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        return None
    if isinstance(raw_time, bool) or not isinstance(raw_time, (int, float)):
        return None
    try:
        when = dt.datetime.fromtimestamp(raw_time / 1_000_000, TAIPEI)
    except (ValueError, OverflowError, OSError):
        return None
    if when.date() != today:
        return None
    return float(price), when


def limit_lock(quote: Any) -> Optional[str]:
    """漲停／跌停鎖住：'limit_up'、'limit_down' 或 None。

    最新價在漲停價且最佳買價也在漲停價（isLimitUpPrice 與 isLimitUpBid）＝鎖漲停；
    跌停對應 isLimitDownPrice 與 isLimitDownAsk。欄位缺漏一律視為沒鎖。
    """
    if not isinstance(quote, dict):
        return None
    if quote.get("isLimitUpPrice") is True and quote.get("isLimitUpBid") is True:
        return "limit_up"
    if quote.get("isLimitDownPrice") is True and quote.get("isLimitDownAsk") is True:
        return "limit_down"
    return None


_LOCK_LABEL = {"limit_up": "鎖漲停", "limit_down": "鎖跌停"}


def describe_quote(quote: Any, today: dt.date) -> Optional[dict]:
    """備援報價判斷：{price, time(HH:MM:SS), lock, note}，今天沒有成交則 None。

    鎖住時成交稀少，最後成交價就是漲／跌停價，仍算有報價，但 note 要明講
    「鎖漲停、最後成交價 X、時間 T」（提醒用途，不是下單依據）。
    """
    got = usable_last_trade(quote, today)
    if got is None:
        return None
    price, when = got
    lock = limit_lock(quote)
    at = when.strftime("%H:%M:%S")
    note = (f"{_LOCK_LABEL[lock]}、最後成交價 {price:g}、時間 {at}" if lock
            else f"最後成交價 {price:g}、時間 {at}")
    return {"price": price, "time": at, "lock": lock, "note": note}

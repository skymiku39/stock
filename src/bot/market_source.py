"""TwsePublicMarketSource -- 從 TWSE 公開延遲報價產生 MarketTick。

TWSE MIS API 回傳的行情相較即時行情延遲 20 分鐘以上，
本模組以輪詢方式將延遲報價轉為 tick-like 序列供 report 模式使用。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import requests

from bot.models import MarketTick
from bot.utils import get_logger, now_tw


class TwsePublicMarketSource:
    """輪詢 TWSE 公開資訊觀測站延遲報價。"""

    MIS_INDEX_URL = "https://mis.twse.com.tw/stock/index.jsp"
    MIS_API_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

    def __init__(
        self,
        symbols: List[str],
        poll_seconds: int = 5,
        logger: Optional[logging.Logger] = None,
    ):
        self.symbols = symbols
        self.poll_seconds = poll_seconds
        self.logger = logger or get_logger("twse-source")

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })

        self._exchange_map: Dict[str, str] = {}
        self._prev_volumes: Dict[str, int] = {}
        self._prev_close: Dict[str, float] = {}
        self._initialized = False

    # ------------------------------------------------------------------
    # Session / 查詢構建
    # ------------------------------------------------------------------

    def _init_session(self) -> None:
        try:
            self._session.get(self.MIS_INDEX_URL, timeout=10)
            self._initialized = True
            self.logger.info("TWSE MIS session 初始化完成")
        except Exception:
            self.logger.exception("TWSE MIS session 初始化失敗")

    def _build_ex_ch(self, symbols: List[str]) -> str:
        parts: List[str] = []
        for s in symbols:
            if s in self._exchange_map:
                parts.append(f"{self._exchange_map[s]}_{s}.tw")
            else:
                parts.append(f"tse_{s}.tw")
                parts.append(f"otc_{s}.tw")
        return "|".join(parts)

    def _fetch_raw(self, symbols: List[str]) -> List[dict]:
        if not self._initialized:
            self._init_session()

        ex_ch = self._build_ex_ch(symbols)
        try:
            resp = self._session.get(
                self.MIS_API_URL,
                params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
                timeout=10,
            )
            data = resp.json()
            return data.get("msgArray", [])
        except Exception:
            self.logger.exception("TWSE 資料拉取失敗")
            return []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_prev_close(self, symbols: List[str]) -> Dict[str, float]:
        """取得昨日收盤價 (y 欄位)。"""
        items = self._fetch_raw(symbols)
        result: Dict[str, float] = {}
        for item in items:
            code = item.get("c", "")
            y_str = item.get("y", "")
            ex = item.get("ex", "")
            if not code or not y_str or y_str == "-":
                continue
            try:
                result[code] = float(y_str)
                self._prev_close[code] = float(y_str)
                if ex:
                    self._exchange_map[code] = ex
            except ValueError:
                pass

        self.logger.info("TWSE 前日收盤: %s", result)
        return result

    def poll(self) -> List[MarketTick]:
        """輪詢一次，回傳有新成交量的 MarketTick 清單。"""
        items = self._fetch_raw(self.symbols)
        ticks: List[MarketTick] = []
        ts = now_tw()

        for item in items:
            code = item.get("c", "")
            z_str = item.get("z", "-")
            v_str = item.get("v", "0")
            y_str = item.get("y", "0")
            ex = item.get("ex", "")

            if not code or z_str == "-":
                continue

            try:
                price = float(z_str)
                volume = int(v_str.replace(",", "")) if v_str != "-" else 0
                prev_close = (
                    float(y_str) if y_str != "-" else self._prev_close.get(code, 0.0)
                )
            except ValueError:
                continue

            if ex and code not in self._exchange_map:
                self._exchange_map[code] = ex
            if prev_close > 0:
                self._prev_close[code] = prev_close

            pct_chg = (
                100 * (price - prev_close) / prev_close if prev_close > 0 else 0.0
            )

            prev_vol = self._prev_volumes.get(code, -1)
            if volume == prev_vol:
                continue
            self._prev_volumes[code] = volume

            ticks.append(
                MarketTick(
                    ts=ts,
                    symbol=code,
                    price=price,
                    volume=volume,
                    pct_chg=pct_chg,
                    prev_close=prev_close,
                    source="twse_public",
                )
            )

        return ticks

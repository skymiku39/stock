"""TwsePublicMarketSource -- 從 TWSE 公開延遲報價產生 MarketTick。

TWSE MIS API 回傳的行情相較即時行情延遲 20 分鐘以上，
本模組以輪詢方式將延遲報價轉為 tick-like 序列供 report 模式使用。
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from bot.models import MarketTick
from bot.utils import get_logger, now_tw


class TwsePublicMarketSource:
    """輪詢 TWSE 公開資訊觀測站延遲報價。"""

    MIS_INDEX_URL = "https://mis.twse.com.tw/stock/index.jsp"
    MIS_API_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

    def __init__(
        self,
        symbols: list[str],
        poll_seconds: int = 5,
        logger: logging.Logger | None = None,
    ):
        self.symbols = symbols
        self.poll_seconds = poll_seconds
        self.logger = logger or get_logger("twse-source")

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        })

        self._exchange_map: dict[str, str] = {}
        self._prev_volumes: dict[str, int] = {}
        self._prev_close: dict[str, float] = {}
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

    def _build_ex_ch(self, symbols: list[str]) -> str:
        parts: list[str] = []
        for s in symbols:
            if s in self._exchange_map:
                parts.append(f"{self._exchange_map[s]}_{s}.tw")
            else:
                parts.append(f"tse_{s}.tw")
                parts.append(f"otc_{s}.tw")
        return "|".join(parts)

    def _fetch_raw(self, symbols: list[str]) -> list[dict]:
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

    @staticmethod
    def _parse_float(value: Any) -> float | None:
        try:
            text = str(value or "").replace(",", "").strip()
            if not text or text == "-":
                return None
            return float(text)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_int(value: Any) -> int:
        try:
            text = str(value or "").replace(",", "").strip()
            if not text or text == "-":
                return 0
            return int(float(text))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _first_book_price(cls, value: Any) -> float | None:
        text = str(value or "").strip()
        if not text or text == "-":
            return None
        return cls._parse_float(text.split("_", 1)[0])

    def get_prev_close(self, symbols: list[str]) -> dict[str, float]:
        """取得昨日收盤價 (y 欄位)。"""
        items = self._fetch_raw(symbols)
        result: dict[str, float] = {}
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

    def get_quotes(self) -> dict[str, dict[str, Any]]:
        """Return latest TWSE MIS quote payloads without suppressing unchanged volume."""
        items = self._fetch_raw(self.symbols)
        ts = now_tw()
        result: dict[str, dict[str, Any]] = {}
        for item in items:
            code = item.get("c", "")
            if not code:
                continue
            z_str = item.get("z", "-")
            v_str = item.get("v", "0")
            y_str = item.get("y", "0")
            pz_str = item.get("pz", "-")
            ex = item.get("ex", "")
            raw_date = str(item.get("d") or "")
            quote_date = ""
            if len(raw_date) == 8 and raw_date.isdigit():
                quote_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"

            last_trade = self._parse_float(z_str)
            previous_trade = self._parse_float(pz_str)
            best_bid = self._first_book_price(item.get("b"))
            best_ask = self._first_book_price(item.get("a"))
            price: float | None = None
            price_basis = ""
            if last_trade is not None:
                price = last_trade
                price_basis = "last_trade"
            elif previous_trade is not None:
                price = previous_trade
                price_basis = "previous_trade"
            elif best_bid is not None and best_ask is not None:
                price = round((best_bid + best_ask) / 2, 4)
                price_basis = "bid_ask_mid"
            elif best_bid is not None:
                price = best_bid
                price_basis = "best_bid"
            elif best_ask is not None:
                price = best_ask
                price_basis = "best_ask"

            prev_close = self._parse_float(y_str) or 0.0
            volume = self._parse_int(v_str)

            pct_chg: float | None = None
            if price is not None and prev_close > 0:
                pct_chg = round(100 * (price - prev_close) / prev_close, 2)
            if ex:
                self._exchange_map[code] = ex
            if prev_close > 0:
                self._prev_close[code] = prev_close

            result[code] = {
                "symbol": code,
                "name": item.get("n", ""),
                "price": price,
                "prev_close": prev_close,
                "pct_chg": pct_chg,
                "volume": volume,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "price_basis": price_basis,
                "quote_date": quote_date,
                "quote_time": item.get("t", ""),
                "exchange": ex,
                "fetched_at": ts.isoformat(timespec="seconds"),
                "source": "twse_mis",
                "raw": item,
            }
        return result

    def poll(self) -> list[MarketTick]:
        """輪詢一次，回傳有新成交量的 MarketTick 清單。"""
        items = self._fetch_raw(self.symbols)
        ticks: list[MarketTick] = []
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

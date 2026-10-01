"""Issue #6：MIS --mis-seconds 新鮮度／盤後假陽性回歸。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bot.validate_sources import (
    assess_mis_quote_freshness,
    check_mis,
)

TW = ZoneInfo("Asia/Taipei")


def _quote(
    *,
    symbol: str,
    price: float,
    volume: int,
    day: str,
    tod: str,
    prev_close: float = 580.0,
) -> Dict[str, Any]:
    raw_date = day.replace("-", "")
    return {
        "symbol": symbol,
        "price": price,
        "volume": volume,
        "prev_close": prev_close,
        "quote_date": day,
        "quote_time": tod,
        "raw": {
            "c": symbol,
            "z": f"{price:.2f}",
            "v": str(volume),
            "y": f"{prev_close:.2f}",
            "d": raw_date,
            "t": tod,
            "ex": "tse",
        },
    }


class TestAssessMisQuoteFreshness:
    def test_after_hours_now_rejected_even_if_quote_at_close(self) -> None:
        # now=13:35 已離開常規時段；13:30 收盤戳不可當 fresh tick。
        quote_dt = dt.datetime(2026, 9, 18, 13, 30, 0, tzinfo=TW)
        now = dt.datetime(2026, 9, 18, 13, 35, 0, tzinfo=TW)
        result = assess_mis_quote_freshness(quote_dt, now=now)
        assert result["now_in_session"] is False
        assert result["session_stamp"] is True
        assert result["fresh"] is False
        assert result["reason"] == "outside_session_now"

    def test_seventeen_minute_stale_in_session_rejected(self) -> None:
        quote_dt = dt.datetime(2026, 9, 18, 10, 15, 0, tzinfo=TW)
        now = dt.datetime(2026, 9, 18, 10, 32, 0, tzinfo=TW)
        result = assess_mis_quote_freshness(quote_dt, now=now)
        assert result["now_in_session"] is True
        assert result["session_stamp"] is True
        assert result["fresh"] is False
        assert result["reason"] == "stale_age"
        assert result["age_seconds"] == 1020
        assert result["max_age_seconds"] == 60

    def test_recent_in_session_accepted(self) -> None:
        quote_dt = dt.datetime(2026, 9, 18, 10, 31, 40, tzinfo=TW)
        now = dt.datetime(2026, 9, 18, 10, 32, 0, tzinfo=TW)
        result = assess_mis_quote_freshness(quote_dt, now=now)
        assert result["now_in_session"] is True
        assert result["session_stamp"] is True
        assert result["fresh"] is True
        assert result["reason"] == "fresh_in_session"
        assert result["age_seconds"] == 20

    def test_stale_same_day_after_hours(self) -> None:
        quote_dt = dt.datetime(2026, 9, 18, 13, 30, 0, tzinfo=TW)
        now = dt.datetime(2026, 9, 18, 16, 5, 0, tzinfo=TW)
        result = assess_mis_quote_freshness(quote_dt, now=now)
        assert result["session_stamp"] is True
        assert result["now_in_session"] is False
        assert result["fresh"] is False
        assert result["reason"] == "outside_session_now"

    def test_weekday_alone_does_not_prove_session(self) -> None:
        # 週四下午：報價戳在盤中窗，但 now 已離場 → 仍非 fresh。
        quote_dt = dt.datetime(2026, 9, 17, 11, 0, 0, tzinfo=TW)  # Thursday
        now = dt.datetime(2026, 9, 17, 15, 0, 0, tzinfo=TW)
        assert now.weekday() == 3
        result = assess_mis_quote_freshness(quote_dt, now=now)
        assert result["fresh"] is False
        assert result["reason"] == "outside_session_now"


class TestCheckMisFreshness:
    def test_after_close_quote_at_1335_not_counted_as_live_tick(self) -> None:
        day = "2026-09-18"
        now = dt.datetime(2026, 9, 18, 13, 35, 0, tzinfo=TW)
        quotes = {
            "2330": _quote(symbol="2330", price=600.0, volume=15000, day=day, tod="13:30:00"),
            "2317": _quote(symbol="2317", price=100.0, volume=8000, day=day, tod="13:30:00"),
        }

        class _Src:
            def get_prev_close(self, _symbols: List[str]) -> Dict[str, float]:
                return {"2330": 580.0, "2317": 98.0}

            def get_quotes(self) -> Dict[str, Dict[str, Any]]:
                return quotes

        with patch("bot.market_source.TwsePublicMarketSource", return_value=_Src()), \
             patch("bot.validate_sources.time.sleep"):
            result = check_mis(3, now=now)

        assert result.ok is True
        assert result.rows == 0
        assert result.details["fresh_tick_count"] == 0
        assert "無新鮮 tick" in result.note or "非盤中" in result.note
        assert all(not q.get("fresh") for q in result.details["quotes"])
        assert all(q.get("quote_time") == "13:30:00" for q in result.details["quotes"])
        assert all(q.get("reason") == "outside_session_now" for q in result.details["quotes"])

    def test_seventeen_minute_stale_in_session_not_counted(self) -> None:
        day = "2026-09-18"
        now = dt.datetime(2026, 9, 18, 10, 32, 0, tzinfo=TW)
        quotes = {
            "2330": _quote(symbol="2330", price=601.0, volume=1200, day=day, tod="10:15:00"),
            "2317": _quote(symbol="2317", price=101.0, volume=900, day=day, tod="10:14:30"),
        }

        class _Src:
            def get_prev_close(self, _symbols: List[str]) -> Dict[str, float]:
                return {"2330": 580.0, "2317": 98.0}

            def get_quotes(self) -> Dict[str, Dict[str, Any]]:
                return quotes

        with patch("bot.market_source.TwsePublicMarketSource", return_value=_Src()), \
             patch("bot.validate_sources.time.sleep"):
            result = check_mis(3, now=now)

        assert result.ok is True
        assert result.rows == 0
        assert result.details["fresh_tick_count"] == 0
        assert all(not q.get("fresh") for q in result.details["quotes"])
        assert all(q.get("reason") == "stale_age" for q in result.details["quotes"])

    def test_recent_in_session_counts_as_live_tick(self) -> None:
        day = "2026-09-18"
        now = dt.datetime(2026, 9, 18, 10, 32, 0, tzinfo=TW)
        quotes = {
            "2330": _quote(symbol="2330", price=601.0, volume=1200, day=day, tod="10:31:40"),
            "2317": _quote(symbol="2317", price=101.0, volume=900, day=day, tod="10:31:50"),
        }

        class _Src:
            def get_prev_close(self, _symbols: List[str]) -> Dict[str, float]:
                return {"2330": 580.0, "2317": 98.0}

            def get_quotes(self) -> Dict[str, Dict[str, Any]]:
                return quotes

        with patch("bot.market_source.TwsePublicMarketSource", return_value=_Src()), \
             patch("bot.validate_sources.time.sleep"):
            result = check_mis(3, now=now)

        assert result.ok is True
        assert result.rows == 2
        assert result.details["fresh_tick_count"] == 2
        assert "新鮮盤中 tick" in result.note
        assert all(q.get("fresh") for q in result.details["quotes"])
        assert result.details["quotes"][0]["quote_at"]
        assert result.details["freshness_rule"]["max_age_seconds"] == 60

    def test_stale_same_day_after_hours_not_counted_as_live_tick(self) -> None:
        day = "2026-09-18"
        now = dt.datetime(2026, 9, 18, 16, 5, 0, tzinfo=TW)
        quotes = {
            "2330": _quote(symbol="2330", price=600.0, volume=15000, day=day, tod="13:30:00"),
            "2317": _quote(symbol="2317", price=100.0, volume=8000, day=day, tod="13:30:00"),
        }

        class _Src:
            def get_prev_close(self, _symbols: List[str]) -> Dict[str, float]:
                return {"2330": 580.0, "2317": 98.0}

            def get_quotes(self) -> Dict[str, Dict[str, Any]]:
                return quotes

        with patch("bot.market_source.TwsePublicMarketSource", return_value=_Src()), \
             patch("bot.validate_sources.time.sleep"):
            result = check_mis(3, now=now)

        assert result.ok is True
        assert result.rows == 0
        assert result.details["fresh_tick_count"] == 0
        assert "無新鮮 tick" in result.note or "非盤中" in result.note
        assert all(not q.get("fresh") for q in result.details["quotes"])
        assert all(q.get("reason") == "outside_session_now" for q in result.details["quotes"])

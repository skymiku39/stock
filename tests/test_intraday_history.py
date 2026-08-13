"""intraday_history 轉換與 DB 寫入測試。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from shioaji.data import Kbars, Ticks

from bot.intraday_history import (
    bars_to_dataframe,
    fetch_and_store_intraday,
    iter_trading_days,
    kbars_to_bars,
    shioaji_ts_to_iso,
    ticks_to_bars,
)
from bot.stock_db import IntradayBar, StockDB


def _ns_at_utc(text: str) -> int:
    """模擬 Shioaji：ns 解析後的 UTC 鐘面 = 台股當地時間。"""
    import pandas as pd
    return int(pd.Timestamp(text, tz="UTC").value)


class TestShioajiConversion:
    def test_shioaji_ts_to_iso(self) -> None:
        iso = shioaji_ts_to_iso(_ns_at_utc("2024-01-02 09:00:00"))
        assert iso == "2024-01-02 09:00:00"

    def test_intraday_ts_legacy_offset(self) -> None:
        from bot.intraday_history import intraday_ts_to_datetime
        fixed = intraday_ts_to_datetime("2026-06-10 17:01:00")
        assert fixed.hour == 9 and fixed.minute == 1

    def test_kbars_to_bars(self) -> None:
        kb = Kbars(
            ts=[_ns_at_utc("2024-01-02 09:00:00"), _ns_at_utc("2024-01-02 09:01:00")],
            Open=[100.0, 101.0],
            High=[101.0, 102.0],
            Low=[99.0, 100.5],
            Close=[100.5, 101.5],
            Volume=[1000, 800],
            Amount=[100500.0, 81200.0],
        )
        bars = kbars_to_bars("2330", kb)
        assert len(bars) == 2
        assert bars[0].symbol == "2330"
        assert bars[0].interval == "1m"
        assert bars[0].close == pytest.approx(100.5)
        assert bars[1].volume == pytest.approx(800)

    def test_ticks_to_bars(self) -> None:
        tk = Ticks(
            ts=[_ns_at_utc("2024-01-02 09:00:01")],
            close=[600.0],
            volume=[5],
            bid_price=[599.0],
            bid_volume=[10],
            ask_price=[600.0],
            ask_volume=[8],
            tick_type=[1],
        )
        bars = ticks_to_bars("2330", tk)
        assert len(bars) == 1
        assert bars[0].interval == "tick"
        assert bars[0].open == bars[0].close == pytest.approx(600.0)
        assert bars[0].volume == pytest.approx(5)


class TestTradingDays:
    def test_skips_weekend(self) -> None:
        import datetime as dt
        days = iter_trading_days(dt.date(2024, 1, 5), dt.date(2024, 1, 8))
        assert days == [dt.date(2024, 1, 5), dt.date(2024, 1, 8)]


class TestIntradayDb:
    @pytest.fixture
    def db(self, tmp_path: Path) -> StockDB:
        return StockDB.open(path=tmp_path / "test.db")

    def test_bulk_upsert_and_query(self, db: StockDB) -> None:
        bars = [
            IntradayBar(
                symbol="2330", ts="2024-01-02 09:00:00",
                interval="1m", open=100, high=101, low=99, close=100.5, volume=1000,
            ),
            IntradayBar(
                symbol="2330", ts="2024-01-02 09:01:00",
                interval="1m", open=101, high=102, low=100.5, close=101.5, volume=800,
            ),
        ]
        n = db.bulk_upsert_intraday_bars(bars)
        assert n == 2
        got = db.get_intraday_bars("2330", start="2024-01-02", end="2024-01-02")
        assert len(got) == 2
        assert got[0].ts == "2024-01-02 09:00:00"
        summary = db.intraday_summary(interval="1m")
        assert summary[0]["rows"] == 2

    def test_bars_to_dataframe(self) -> None:
        bars = [
            IntradayBar(symbol="2330", ts="2024-01-02 09:00:00", close=100.0),
        ]
        df = bars_to_dataframe(bars)
        assert len(df) == 1
        assert df.iloc[0]["close"] == pytest.approx(100.0)


class TestFetchAndStore:
    def test_fetch_and_store_with_mock_broker(self, tmp_path: Path) -> None:
        import datetime as dt

        from shioaji.data import Kbars

        broker = MagicMock()
        broker.fetch_kbars.return_value = Kbars(
            ts=[_ns_at_utc("2024-01-02 09:00:00")],
            Open=[100.0], High=[101.0], Low=[99.0], Close=[100.5],
            Volume=[1000], Amount=[100500.0],
        )
        db = StockDB.open(path=tmp_path / "test.db")
        written, skipped = fetch_and_store_intraday(
            broker,
            "2330",
            dt.date(2024, 1, 2),
            dt.date(2024, 1, 2),
            interval="1m",
            save_csv=True,
            root=tmp_path,
            db=db,
            request_delay_sec=0,
        )
        assert written == 1
        assert skipped == 0
        assert db.count_intraday_bars("2330", start="2024-01-02") == 1
        csv = tmp_path / "data" / "intraday" / "2330" / "1m_2024-01-02.csv"
        assert csv.exists()

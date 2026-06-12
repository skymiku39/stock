"""daily_backtest 單元測試。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.config import Settings
from bot.daily_backtest import DailyBacktester
from bot.stock_db import PriceBar, StockDB


@pytest.fixture
def db(tmp_path: Path) -> StockDB:
    return StockDB.open(path=tmp_path / "test.db")


class TestDailyBacktest:
    def test_rally_day_entry(self, db: StockDB) -> None:
        days = [
            PriceBar(symbol="2330", date="2026-06-08", open=100, high=100, low=99, close=100),
            PriceBar(symbol="2330", date="2026-06-09", open=101, high=104, low=100.5, close=103),
            PriceBar(symbol="2330", date="2026-06-10", open=103, high=106, low=102, close=105),
        ]
        db.bulk_upsert_price_bars(days)
        settings = Settings(
            min_pct_chg_on_entry=1.0,
            max_pct_chg_on_entry=5.0,
            stop_loss_pct=-5.0,
            max_fund=500_000,
            max_lot_per_symbol=1,
        )
        result = DailyBacktester(settings).run_symbol(
            db, "2330", start="2026-06-09", end="2026-06-10",
        )
        assert result.bar_days >= 1
        assert len(result.trades) >= 1

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
    def test_gap_above_entry_band_does_not_create_untraded_price(self):
        settings = Settings(_env_file=None, min_pct_chg_on_entry=1, max_pct_chg_on_entry=5)
        bar = PriceBar('2330', '2026-11-05', open=110, high=112, low=109, close=111)
        assert DailyBacktester(settings)._resolve_entry_price('2330', 100, bar) is None

    def test_first_requested_session_uses_prior_close_and_etf_tax(self, db):
        db.bulk_upsert_price_bars([
            PriceBar('0050', '2026-11-04', open=100, high=100, low=100, close=100),
            PriceBar('0050', '2026-11-05', open=101, high=103, low=101, close=102),
        ])
        settings = Settings(_env_file=None, buy_entry_targets='', min_pct_chg_on_entry=1,
            max_pct_chg_on_entry=5, stop_loss_pct=-10, take_profit_pct=100,
            max_fund=200000, daily_fund_budget=0, max_lot_per_symbol=1)
        result = DailyBacktester(settings).run_symbol(db, '0050', start='2026-11-05', end='2026-11-05')
        assert len(result.trades) == 1
        trade = result.trades[0]
        # Independent ledger: 1000 shares, 0.1425% * 0.28 fees, ETF sell tax 0.1%.
        expected = 102000 - 102000 * .001425 * .28 - 102000 * .001 - (101000 + 101000 * .001425 * .28)
        assert trade.pnl_twd == pytest.approx(expected)

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

"""intraday_backtest 單元測試。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from bot.config import Settings
from bot.intraday_backtest import IntradayBacktester
from bot.stock_db import IntradayBar, PriceBar, StockDB


@pytest.fixture
def db(tmp_path: Path) -> StockDB:
    return StockDB.open(path=tmp_path / "test.db")


def _seed_day(db: StockDB, symbol: str, day: str, prev_close: float) -> None:
    db.bulk_upsert_price_bars([
        PriceBar(symbol=symbol, date=day, close=prev_close, open=prev_close),
    ])
    bars = []
    for minute in range(0, 30):
        t = dt.time(9, minute)
        ts = f"{day} {t.hour:02d}:{t.minute:02d}:00"
        px = prev_close * (1 + 0.02 * minute / 30)
        bars.append(IntradayBar(
            symbol=symbol, ts=ts, interval="1m",
            open=px, high=px, low=px, close=px, volume=100,
        ))
    close_ts = f"{day} 13:15:00"
    bars.append(IntradayBar(
        symbol=symbol, ts=close_ts, interval="1m",
        open=px, high=px, low=px, close=px, volume=100,
    ))
    db.bulk_upsert_intraday_bars(bars)


class TestIntradayBacktest:
    def test_truncated_session_does_not_fabricate_closing_fill(self, db: StockDB) -> None:
        db.bulk_upsert_price_bars([PriceBar(symbol='2330', date='2026-06-09', close=100)])
        db.bulk_upsert_intraday_bars([IntradayBar(symbol='2330', ts='2026-06-10 09:01:00',
            interval='1m', open=100, high=100, low=100, close=100, volume=10)])
        result = IntradayBacktester(Settings(_env_file=None, min_pct_chg_on_entry=-1,
            max_pct_chg_on_entry=1, max_fund=500000, daily_fund_budget=0)).run_symbol(db, '2330')
        assert result.trades == []
        assert result.open_positions[0]['entry_ts'] == '2026-06-10 09:01:00'
        assert result.skip_reason == 'incomplete_session_open_position'

    def test_first_intraday_close_cannot_replace_previous_close(self, db: StockDB) -> None:
        db.bulk_upsert_intraday_bars([IntradayBar(symbol='2330', ts='2026-06-10 09:01:00', close=100)])
        with pytest.raises(ValueError, match='previous daily close missing'):
            IntradayBacktester(Settings(_env_file=None)).run_symbol(db, '2330')

    def test_entry_and_exit_on_rally(self, db: StockDB) -> None:
        symbol = "2330"
        day = "2026-06-10"
        _seed_day(db, symbol, "2026-06-09", 100.0)
        _seed_day(db, symbol, day, 100.0)

        settings = Settings(
            min_pct_chg_on_entry=1.0,
            max_pct_chg_on_entry=5.0,
            stop_loss_pct=-5.0,
            take_profit_pct=1.0,
            trailing_stop_pct=0.5,
            max_fund=500_000,
            max_lot_per_symbol=1,
        )
        engine = IntradayBacktester(settings)
        result = engine.run_symbol(db, symbol, start=day, end=day)
        assert result.bar_days == 1
        assert len(result.trades) >= 1
        assert result.trades[0].symbol == symbol

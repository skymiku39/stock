"""微笑曲線策略回測測試。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.config import Settings
from bot.smile_curve import SmileCurveEngine, parse_smile_buy_tiers
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.stock_db import PriceBar, StockDB
from bot.trade_cost import net_pnl_pct


@pytest.fixture
def db(tmp_path: Path) -> StockDB:
    return StockDB.open(path=tmp_path / "smile.db")


def _u_shape_bars(symbol: str = "2330") -> list[PriceBar]:
    """U 型：100 → 跌到 82 → 彈回 102。"""
    closes = [100, 99, 97, 94, 90, 86, 83, 82, 84, 88, 93, 98, 102]
    bars: list[PriceBar] = []
    for i, c in enumerate(closes):
        d = f"2026-01-{i + 2:02d}"
        bars.append(PriceBar(
            symbol=symbol, date=d,
            open=c, high=c + 1, low=c - 1, close=float(c),
        ))
    return bars


class TestSmileCurveEngine:
    def test_parse_tiers(self) -> None:
        tiers = parse_smile_buy_tiers("1:1,3:2,5:3")
        assert len(tiers) == 3
        assert tiers[0].drop_pct == 1.0
        assert tiers[-1].multiplier == 3

    def test_u_shape_produces_buys_and_profitable_sells(self) -> None:
        settings = Settings(
            smile_buy_tiers="1:1,3:2,5:2",
            smile_base_lot=1,
            max_fund=500_000,
            max_lot_per_symbol=10,
            _env_file=None,
        )
        engine = SmileCurveEngine("2330", settings)
        for bar in _u_shape_bars():
            engine.on_bar(bar.date[:10], float(bar.close))

        assert len(engine.round_trips) >= 1
        assert all(t.pnl_twd > 0 for t in engine.round_trips)
        assert all(t.pnl_pct > 0 for t in engine.round_trips)

    def test_never_sells_at_loss_on_rebound(self) -> None:
        settings = Settings(
            smile_buy_tiers="1:1,5:2",
            smile_base_lot=1,
            max_fund=500_000,
            max_lot_per_symbol=5,
            _env_file=None,
        )
        engine = SmileCurveEngine("2330", settings)
        # 買在 90
        engine.on_bar("2026-01-02", 100.0)
        engine.on_bar("2026-01-03", 90.0)
        # 反彈到 91 — 低於成本，不應賣
        engine.on_bar("2026-01-04", 91.0)
        assert len(engine.round_trips) == 0
        assert len(engine.open_lots) >= 1


class TestSmileCurveBacktest:
    def test_backtest_on_synthetic_u_shape(self, db: StockDB) -> None:
        db.bulk_upsert_price_bars(_u_shape_bars())
        settings = Settings(
            smile_buy_tiers="1:1,3:2,5:2,8:2",
            smile_base_lot=1,
            max_fund=800_000,
            max_lot_per_symbol=8,
            _env_file=None,
        )
        result = SmileCurveBacktester(settings).run_symbol(
            db, "2330", start="2026-01-02", end="2026-01-14",
        )
        assert result.bar_count >= 10
        assert result.trade_count >= 1
        assert result.total_pnl_twd > 0
        assert result.win_rate == 100.0

    def test_fixed_reference_price(self, db: StockDB) -> None:
        bars = [
            PriceBar(symbol="0050", date="2026-02-01", open=200, high=200, low=198, close=200),
            PriceBar(symbol="0050", date="2026-02-02", open=198, high=198, low=190, close=192),
            PriceBar(symbol="0050", date="2026-02-03", open=192, high=195, low=188, close=188),
            PriceBar(symbol="0050", date="2026-02-04", open=188, high=201, low=188, close=200),
        ]
        db.bulk_upsert_price_bars(bars)
        settings = Settings(
            smile_reference_prices={"0050": 200.0},
            smile_buy_tiers="2:1,6:2",
            smile_base_lot=1,
            max_fund=500_000,
            _env_file=None,
        )
        result = SmileCurveBacktester(settings).run_symbol(db, "0050")
        for trip in result.round_trips:
            assert net_pnl_pct(
                trip.entry_price, trip.exit_price,
                trip.quantity, trip.unit,
                settings=settings,
                sell_tax_rate=settings.smile_regular_tax_rate,
            ) > 0

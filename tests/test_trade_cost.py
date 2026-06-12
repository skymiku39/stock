"""交易成本與回落買回測試。"""

from __future__ import annotations

import pytest

from bot.config import Settings
from bot.trade_cost import (
    TradeCostParams,
    buy_cash_required,
    max_affordable_qty,
    net_pnl_pct,
    net_pnl_twd,
    rebuy_opportunity,
    sell_cash_received,
)


def _settings(**kw) -> Settings:
    defaults = dict(symbols=["2330"], _env_file=None)
    defaults.update(kw)
    return Settings(**defaults)


class TestTradeCost:
    def test_100k_round_trip_has_fees(self) -> None:
        p = TradeCostParams(fee_discount=0.28, min_fee=1.0, day_trade_tax_rate=0.0015)
        buy = buy_cash_required(100.0, 1, "lot", p)
        assert buy > 100_000
        recv = sell_cash_received(102.0, 1, "lot", p)
        assert recv < 102_000
        pnl = net_pnl_twd(100.0, 102.0, 1, "lot", p)
        gross = (102 - 100) * 1000
        assert pnl < gross

    def test_net_pnl_pct_positive_on_rise(self) -> None:
        p = TradeCostParams()
        pct = net_pnl_pct(50.0, 52.0, 10, "share", p)
        assert pct > 0

    def test_rebuy_when_dipped_from_exit(self) -> None:
        s = _settings(
            allow_same_day_reentry=True,
            rebuy_target_net_pct=2.0,
            broker_fee_discount=0.28,
        )
        assert rebuy_opportunity(100.0, 97.0, 50, "share", settings=s) is True

    def test_rebuy_rejects_price_above_exit(self) -> None:
        s = _settings(allow_same_day_reentry=True, rebuy_target_net_pct=2.0)
        assert rebuy_opportunity(100.0, 101.0, 50, "share", settings=s) is False

    def test_rebuy_disabled(self) -> None:
        s = _settings(allow_same_day_reentry=False)
        assert rebuy_opportunity(100.0, 95.0, 50, "share", settings=s) is False

    def test_max_affordable_qty_respects_fees(self) -> None:
        s = _settings(broker_fee_discount=0.28, broker_min_fee=1.0)
        budget = 10_000.0
        price = 200.0
        shares = max_affordable_qty(
            price, budget, "share", max_qty=999, settings=s,
        )
        assert shares >= 1
        assert buy_cash_required(price, shares, "share", settings=s) <= budget
        assert buy_cash_required(price, shares + 1, "share", settings=s) > budget

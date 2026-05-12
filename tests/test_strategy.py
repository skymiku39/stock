"""策略模式測試 -- 確認 watch/report 不下單、虛擬部位正確運作。"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from bot.config import Settings
from bot.models import MarketTick, PositionInfo
from bot.strategy import MyStrategy


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        symbols=["2330"],
        run_mode="watch",
        api_key="test",
        secret_key="test",
        _env_file=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


def _make_tick(
    symbol: str = "2330",
    price: float = 600.0,
    prev_close: float = 580.0,
    source: str = "shioaji",
) -> MarketTick:
    pct_chg = 100 * (price - prev_close) / prev_close if prev_close else 0
    return MarketTick(
        ts=datetime.datetime(2026, 5, 12, 9, 15),
        symbol=symbol,
        price=price,
        volume=100,
        pct_chg=pct_chg,
        prev_close=prev_close,
        source=source,
    )


class TestWatchModeNoOrders:
    """watch 模式不應呼叫 broker 的下單方法。"""

    def test_place_buy_records_signal_not_order(self) -> None:
        settings = _make_settings(run_mode="watch")
        broker = MagicMock()
        strategy = MyStrategy(broker=broker, settings=settings)
        strategy._prev_close["2330"] = 580.0

        result = strategy._place_buy("2330", 600.0, 1, "enter")

        assert result is True
        broker.place_order.assert_not_called()
        assert strategy.signal_recorder.signal_count == 1
        assert "2330" in strategy._enter_placed

    def test_place_stop_sell_records_signal_not_order(self) -> None:
        settings = _make_settings(run_mode="watch")
        broker = MagicMock()
        strategy = MyStrategy(broker=broker, settings=settings)

        strategy.positions["2330"] = PositionInfo(
            symbol="2330", avg_price=580.0, quantity=1,
        )
        strategy._last_price["2330"] = 560.0

        result = strategy._place_stop_sell("2330", 1, "sl")

        assert result is True
        broker.place_market_sell.assert_not_called()
        assert strategy.signal_recorder.signal_count == 1
        assert "2330" not in strategy.positions

    def test_has_pending_always_false(self) -> None:
        settings = _make_settings(run_mode="watch")
        strategy = MyStrategy(broker=MagicMock(), settings=settings)
        assert strategy._has_pending("2330") is False


class TestReportModeNoOrders:
    """report 模式不應觸碰 broker。"""

    def test_place_buy_without_broker(self) -> None:
        settings = _make_settings(run_mode="report")
        strategy = MyStrategy(broker=None, settings=settings)
        strategy._prev_close["2330"] = 580.0

        result = strategy._place_buy("2330", 600.0, 1, "enter")
        assert result is True
        assert strategy.signal_recorder.signal_count == 1


class TestVirtualPosition:
    """虛擬部位追蹤。"""

    def test_buy_creates_position(self) -> None:
        settings = _make_settings(run_mode="watch")
        strategy = MyStrategy(broker=MagicMock(), settings=settings)
        strategy._prev_close["2330"] = 580.0

        strategy._virtual_fill_buy("2330", 600.0, 2, "enter")

        assert "2330" in strategy.positions
        pos = strategy.positions["2330"]
        assert pos.avg_price == 600.0
        assert pos.quantity == 2
        assert strategy._fund_used == 600.0 * 2 * 1000

    def test_sell_closes_position(self) -> None:
        settings = _make_settings(run_mode="watch")
        strategy = MyStrategy(broker=MagicMock(), settings=settings)
        strategy._prev_close["2330"] = 580.0

        strategy._virtual_fill_buy("2330", 600.0, 1, "enter")
        strategy._virtual_fill_sell("2330", 610.0, 1, "trail")

        assert "2330" not in strategy.positions
        assert strategy._fund_used == 0.0

        events = strategy.signal_recorder._signals
        sell_event = [e for e in events if e.action == "would-sell"][0]
        assert sell_event.pnl_pct == pytest.approx(
            100 * (610.0 - 600.0) / 600.0, rel=1e-4,
        )

    def test_stop_loss_triggers_virtual_sell(self) -> None:
        settings = _make_settings(
            run_mode="watch", stop_loss_pct=-3.0,
            enter_cutoff_time="09:30", exit_time="13:15",
        )
        strategy = MyStrategy(broker=MagicMock(), settings=settings)
        strategy._prev_close["2330"] = 580.0
        strategy._last_price["2330"] = 560.0

        strategy.positions["2330"] = PositionInfo(
            symbol="2330", avg_price=580.0, quantity=1,
        )

        with patch("bot.strategy.now_tw_time") as mock_time:
            mock_time.return_value = datetime.time(10, 0)
            tick = _make_tick(symbol="2330", price=560.0, prev_close=580.0)
            strategy.on_tick(tick)

        signals = strategy.signal_recorder._signals
        sells = [s for s in signals if s.action == "would-sell"]
        assert len(sells) == 1
        assert sells[0].reason == "sl"


class TestTradeModeRegression:
    """trade 模式應繼續呼叫 broker 下單。"""

    def test_place_buy_calls_broker(self) -> None:
        settings = _make_settings(run_mode="trade")
        broker = MagicMock()
        mock_trade = MagicMock()
        mock_trade.order.ordno = "TEST001"
        broker.place_order.return_value = mock_trade

        strategy = MyStrategy(broker=broker, settings=settings)

        result = strategy._place_buy("2330", 600.0, 1, "enter")

        assert result is True
        broker.place_order.assert_called_once()
        assert strategy.signal_recorder.signal_count == 0

"""策略模式測試 -- 確認 watch/report 不下單、虛擬部位正確運作。"""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.config import Settings
from bot.llm_gate import LlmGate
from bot.models import MarketTick, PositionInfo
from bot.ownership import BOT_BUY_FIELD, bot_sell_field
from bot.entry_rules import in_entry_range, resolve_entry_range
from bot.risk_guard import RiskGuard
from bot.strategy import SymbolExitRecord
from bot.trade_cost import buy_cash_required, position_net_pnl_pct
from bot.strategy_configurable import ConfigurableStrategy


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        symbols=["2330"],
        run_mode="watch",
        api_key="test",
        secret_key="test",
        # 測試固定股價 600，所以 max_fund 給足，否則 RiskGuard 會擋下
        max_fund=1_000_000,
        daily_fund_budget=0,
        llm_gate_enabled=False,
        llm_sell_gate_enabled=False,
        _env_file=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


def _freeze_tw_time(t: datetime.time):
    """統一 mock 策略使用的台灣時間。"""
    return (
        patch("bot.strategy_configurable.now_tw_time", return_value=t),
        patch("bot.strategy.now_tw_time", return_value=t),
    )


def _bind_isolated_risk(strategy, settings: Settings, tmp_path: Path) -> None:
    """避免測試讀寫專案 data/risk_state，並與 LlmGate 共用同一 RiskGuard。"""
    strategy.risk = RiskGuard(settings, project_root=tmp_path)
    strategy._fund_used = strategy.risk.fund_used
    strategy.llm_gate = LlmGate(
        settings, risk=strategy.risk, project_root=tmp_path,
    )


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
        strategy = ConfigurableStrategy(broker=broker, settings=settings)
        strategy._prev_close["2330"] = 580.0

        result = strategy._place_buy("2330", 600.0, 1, "enter")

        assert result is True
        broker.place_order.assert_not_called()
        assert strategy.signal_recorder.signal_count == 1
        assert "2330" in strategy._enter_placed

    def test_place_stop_sell_records_signal_not_order(self) -> None:
        settings = _make_settings(run_mode="watch")
        broker = MagicMock()
        strategy = ConfigurableStrategy(broker=broker, settings=settings)

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
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        assert strategy._has_pending("2330") is False


class TestReportModeNoOrders:
    """report 模式不應觸碰 broker。"""

    def test_place_buy_without_broker(self) -> None:
        settings = _make_settings(run_mode="report")
        strategy = ConfigurableStrategy(broker=None, settings=settings)
        strategy._prev_close["2330"] = 580.0

        result = strategy._place_buy("2330", 600.0, 1, "enter")
        assert result is True
        assert strategy.signal_recorder.signal_count == 1


class TestVirtualPosition:
    """虛擬部位追蹤。"""

    def test_buy_creates_position(self, tmp_path: Path) -> None:
        settings = _make_settings(run_mode="watch")
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        _bind_isolated_risk(strategy, settings, tmp_path)
        strategy._prev_close["2330"] = 580.0

        strategy._virtual_fill_buy("2330", 600.0, 2, "enter")

        assert "2330" in strategy.positions
        pos = strategy.positions["2330"]
        assert pos.avg_price == 600.0
        assert pos.quantity == 2
        assert strategy._fund_used == buy_cash_required(600.0, 2, "lot", settings=settings)

    def test_sell_closes_position(self, tmp_path: Path) -> None:
        settings = _make_settings(run_mode="watch")
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        _bind_isolated_risk(strategy, settings, tmp_path)
        strategy._prev_close["2330"] = 580.0

        strategy._virtual_fill_buy("2330", 600.0, 1, "enter")
        strategy._virtual_fill_sell("2330", 610.0, 1, "trail")

        assert "2330" not in strategy.positions
        assert strategy._fund_used == 0.0

        events = strategy.signal_recorder._signals
        sell_event = [e for e in events if e.action == "would-sell"][0]
        expected = position_net_pnl_pct(600.0, 610.0, 1, "lot", settings=settings)
        assert sell_event.pnl_pct == pytest.approx(expected, rel=1e-4)

    def test_stop_loss_triggers_virtual_sell(self) -> None:
        settings = _make_settings(
            run_mode="watch", stop_loss_pct=-3.0,
            enter_cutoff_time="09:30", exit_time="13:15",
        )
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy._prev_close["2330"] = 580.0
        strategy._last_price["2330"] = 560.0

        strategy.positions["2330"] = PositionInfo(
            symbol="2330", avg_price=580.0, quantity=1,
        )

        with (
            patch("bot.strategy_configurable.now_tw_time", return_value=datetime.time(10, 0)),
            patch("bot.strategy.now_tw_time", return_value=datetime.time(10, 0)),
        ):
            tick = _make_tick(symbol="2330", price=560.0, prev_close=580.0)
            strategy.on_tick(tick)

        signals = strategy.signal_recorder._signals
        sells = [s for s in signals if s.action == "would-sell"]
        assert len(sells) == 1
        assert sells[0].reason == "sl"

    def test_user_sell_target_triggers_virtual_sell(self) -> None:
        settings = _make_settings(
            run_mode="watch",
            sell_profit_targets={"2330": 4.0},
            enter_cutoff_time="09:30",
            exit_time="13:15",
        )
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy._prev_close["2330"] = 100.0
        strategy.positions["2330"] = PositionInfo(
            symbol="2330", avg_price=100.0, quantity=1,
        )

        with (
            patch("bot.strategy_configurable.now_tw_time", return_value=datetime.time(10, 0)),
            patch("bot.strategy.now_tw_time", return_value=datetime.time(10, 0)),
        ):
            strategy.on_tick(_make_tick(symbol="2330", price=106.0, prev_close=100.0))

        sells = [s for s in strategy.signal_recorder._signals if s.action == "would-sell"]
        assert len(sells) == 1
        assert sells[0].reason == "target"
        assert "2330" not in strategy.positions

    def test_user_sell_target_blocks_other_sell_before_target(self) -> None:
        settings = _make_settings(
            run_mode="watch",
            sell_profit_targets={"2330": 8.0},
            stop_loss_pct=-3.0,
            enter_cutoff_time="09:30",
            exit_time="13:15",
        )
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy._prev_close["2330"] = 100.0
        strategy.positions["2330"] = PositionInfo(
            symbol="2330", avg_price=100.0, quantity=1,
        )

        with (
            patch("bot.strategy_configurable.now_tw_time", return_value=datetime.time(10, 0)),
            patch("bot.strategy.now_tw_time", return_value=datetime.time(10, 0)),
        ):
            strategy.on_tick(_make_tick(symbol="2330", price=96.0, prev_close=100.0))

        assert strategy.signal_recorder.signal_count == 0
        assert "2330" in strategy.positions

    def test_llm_sell_gate_blocks_virtual_sell(self, tmp_path: Path) -> None:
        _write_llm = tmp_path / "data" / "auto_llm"
        _write_llm.mkdir(parents=True)
        (_write_llm / "2330.json").write_text(
            '{"sentiment":"positive","sentiment_score":0.6,'
            '"confidence":0.8,"fetched_at":"2026-06-09T08:00:00"}',
            encoding="utf-8",
        )
        settings = _make_settings(
            run_mode="watch",
            llm_sell_gate_enabled=True,
            llm_sell_gate_bypass_stop_loss=False,
            stop_loss_pct=-3.0,
            enter_cutoff_time="09:30",
            exit_time="13:15",
        )
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy.risk = RiskGuard(settings, project_root=tmp_path)
        strategy.llm_gate = LlmGate(
            settings, risk=strategy.risk, project_root=tmp_path,
        )
        strategy._prev_close["2330"] = 100.0
        strategy.positions["2330"] = PositionInfo(
            symbol="2330", avg_price=100.0, quantity=1,
        )

        with (
            patch("bot.strategy_configurable.now_tw_time", return_value=datetime.time(10, 0)),
            patch("bot.strategy.now_tw_time", return_value=datetime.time(10, 0)),
        ):
            strategy.on_tick(_make_tick(symbol="2330", price=96.0, prev_close=100.0))

        assert "2330" in strategy.positions
        blocked = [
            s for s in strategy.signal_recorder._signals
            if s.action == "sell-blocked"
        ]
        assert len(blocked) == 1

    def test_trailing_skipped_when_limit_up_potential(self, tmp_path: Path) -> None:
        llm_dir = tmp_path / "data" / "auto_llm"
        llm_dir.mkdir(parents=True)
        (llm_dir / "2330.json").write_text(
            '{"limit_up_potential":true,"fetched_at":"2026-06-09T08:00:00"}',
            encoding="utf-8",
        )
        settings = _make_settings(
            run_mode="watch",
            take_profit_pct=3.0,
            trailing_stop_pct=2.0,
            enter_cutoff_time="09:30",
            exit_time="13:15",
        )
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy.llm_gate = LlmGate(settings, project_root=tmp_path)
        strategy._prev_close["2330"] = 100.0
        strategy.positions["2330"] = PositionInfo(
            symbol="2330", avg_price=100.0, quantity=1,
        )
        strategy._peak_net_pnl["2330"] = 8.0

        with (
            patch("bot.strategy_configurable.now_tw_time", return_value=datetime.time(10, 0)),
            patch("bot.strategy.now_tw_time", return_value=datetime.time(10, 0)),
        ):
            strategy.on_tick(_make_tick(symbol="2330", price=105.0, prev_close=100.0))

        assert "2330" in strategy.positions
        assert strategy.signal_recorder.signal_count == 0

    def test_reentry_after_exit(self, tmp_path: Path) -> None:
        settings = _make_settings(
            run_mode="watch",
            allow_same_day_reentry=True,
            rebuy_target_net_pct=2.0,
            min_pct_chg_on_entry=0.0,
            max_pct_chg_on_entry=10.0,
            llm_gate_enabled=False,
            enter_cutoff_time="09:30",
            exit_time="13:15",
        )
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        _bind_isolated_risk(strategy, settings, tmp_path)
        strategy._prev_close["2330"] = 100.0
        strategy._symbol_exits["2330"] = SymbolExitRecord(
            exit_price=100.0,
            exit_net_pnl_pct=3.0,
            exit_time=datetime.datetime.now(),
            quantity=1,
            unit="lot",
        )

        with (
            patch("bot.strategy_configurable.now_tw_time", return_value=datetime.time(10, 0)),
            patch("bot.strategy.now_tw_time", return_value=datetime.time(10, 0)),
        ):
            strategy.on_tick(_make_tick(symbol="2330", price=97.0, prev_close=100.0))

        buys = [s for s in strategy.signal_recorder._signals if s.action == "would-buy"]
        assert len(buys) >= 1

    def test_non_ai_position_cannot_auto_sell(self) -> None:
        settings = _make_settings(run_mode="watch")
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy.positions["2330"] = PositionInfo(
            symbol="2330", avg_price=100.0, quantity=1, owner_tag="MANUAL",
        )
        strategy._last_price["2330"] = 110.0

        result = strategy._place_stop_sell("2330", 1, "trail")

        assert result is False
        assert strategy.signal_recorder.signal_count == 0
        assert "2330" in strategy.positions

    def test_afternoon_profit_exit_without_trailing_threshold(self, tmp_path: Path) -> None:
        llm_dir = tmp_path / "data" / "auto_llm"
        llm_dir.mkdir(parents=True)
        (llm_dir / "2330.json").write_text(
            '{"sentiment":"positive","sentiment_score":0.7,'
            '"confidence":0.9,"fetched_at":"2026-06-09T08:00:00"}',
            encoding="utf-8",
        )
        settings = _make_settings(
            run_mode="watch",
            take_profit_pct=6.0,
            trailing_stop_pct=2.0,
            profit_exit_start_time="12:50",
            exit_time="13:00",
            enter_cutoff_time="09:30",
            llm_sell_gate_enabled=True,
            llm_sell_gate_bypass_afternoon=True,
        )
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy.llm_gate = LlmGate(
            settings, risk=strategy.risk, project_root=tmp_path,
        )
        strategy._prev_close["2330"] = 100.0
        strategy.positions["2330"] = PositionInfo(
            symbol="2330", avg_price=100.0, quantity=1,
        )

        with (
            patch("bot.strategy_configurable.now_tw_time", return_value=datetime.time(12, 55)),
            patch("bot.strategy.now_tw_time", return_value=datetime.time(12, 55)),
        ):
            strategy.on_tick(_make_tick(symbol="2330", price=102.0, prev_close=100.0))

        assert "2330" not in strategy.positions
        sells = [
            s for s in strategy.signal_recorder._signals
            if s.action == "would-sell"
        ]
        assert len(sells) == 1
        assert sells[0].reason == "afternoon"

    def test_before_afternoon_window_keeps_small_profit(self) -> None:
        settings = _make_settings(
            run_mode="watch",
            take_profit_pct=6.0,
            trailing_stop_pct=2.0,
            profit_exit_start_time="12:50",
            exit_time="13:00",
            enter_cutoff_time="09:30",
        )
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy._prev_close["2330"] = 100.0
        strategy.positions["2330"] = PositionInfo(
            symbol="2330", avg_price=100.0, quantity=1,
        )

        with (
            patch("bot.strategy_configurable.now_tw_time", return_value=datetime.time(11, 0)),
            patch("bot.strategy.now_tw_time", return_value=datetime.time(11, 0)),
        ):
            strategy.on_tick(_make_tick(symbol="2330", price=102.0, prev_close=100.0))

        assert "2330" in strategy.positions
        assert strategy.signal_recorder.signal_count == 0


class TestOddLotQuantity:
    def test_calc_quantity_returns_shares_for_small_fund(self) -> None:
        settings = _make_settings(max_fund=10_000, use_odd_lot=True)
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        qty, unit = strategy._calc_quantity(600.0)
        assert unit == "share"
        assert qty >= 1
        assert buy_cash_required(600.0, qty, unit, settings=settings) <= 10_000

    def test_virtual_fill_share_cost(self) -> None:
        settings = _make_settings(run_mode="watch", max_fund=10_000)
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy._virtual_fill_buy("2330", 50.0, 100, "enter", unit="share")
        assert strategy._fund_used == buy_cash_required(50.0, 100, "share", settings=settings)
        assert strategy.positions["2330"].unit == "share"


class TestConfigurableStrategy:
    def test_entry_range_from_env(self) -> None:
        settings = _make_settings(
            min_pct_chg_on_entry=2.0,
            max_pct_chg_on_entry=4.0,
        )
        assert resolve_entry_range("2330", settings) == (2.0, 4.0)
        assert in_entry_range("2330", 3.0, settings) is True
        assert in_entry_range("2330", 1.5, settings) is False
        assert in_entry_range("2330", 2.0, settings) is True
        assert in_entry_range("2330", 1.0, _make_settings(min_pct_chg_on_entry=1.0, max_pct_chg_on_entry=5.0)) is True

    def test_per_symbol_entry_override(self) -> None:
        settings = _make_settings(
            buy_entry_targets={"2330": (0.5, 3.0)},
        )
        assert resolve_entry_range("2330", settings) == (0.5, 3.0)

    def test_configurable_entry_with_llm_gate_off(self) -> None:
        settings = _make_settings(
            run_mode="watch",
            max_fund=1_000_000,
            min_pct_chg_on_entry=1.0,
            max_pct_chg_on_entry=5.0,
            llm_gate_enabled=False,
            strategy_type="configurable",
        )
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy._prev_close["2330"] = 580.0

        with patch("bot.strategy_configurable.now_tw_time") as mock_time:
            mock_time.return_value = datetime.time(9, 15)
            tick = _make_tick(symbol="2330", price=600.0, prev_close=580.0)
            strategy.on_tick(tick)

        buys = [s for s in strategy.signal_recorder._signals if s.action == "would-buy"]
        assert len(buys) == 1

    def test_intraday_entry_after_morning_cutoff(self) -> None:
        """盤中 10:30 漲幅達標仍可進場（不受舊 09:30 截止限制）。"""
        settings = _make_settings(
            run_mode="watch",
            max_fund=1_000_000,
            min_pct_chg_on_entry=1.0,
            max_pct_chg_on_entry=5.0,
            llm_gate_enabled=False,
            strategy_type="configurable",
            enter_cutoff_time="09:30",
            exit_time="13:00",
        )
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy._prev_close["2330"] = 580.0

        with patch("bot.strategy_configurable.now_tw_time") as mock_time:
            mock_time.return_value = datetime.time(10, 30)
            strategy.on_tick(_make_tick(symbol="2330", price=600.0, prev_close=580.0))

        buys = [s for s in strategy.signal_recorder._signals if s.action == "would-buy"]
        assert len(buys) == 1
        assert buys[0].reason == "enter"

    def test_intraday_reenter_after_sell(self) -> None:
        """平倉後漲幅再達標 → 盤中再進。"""
        settings = _make_settings(
            run_mode="watch",
            max_fund=1_000_000,
            min_pct_chg_on_entry=1.0,
            max_pct_chg_on_entry=5.0,
            llm_gate_enabled=False,
            strategy_type="configurable",
            exit_time="13:00",
            reentry_cooldown_seconds=0,
        )
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        strategy._prev_close["2330"] = 580.0

        with patch("bot.strategy_configurable.now_tw_time") as mock_time:
            mock_time.return_value = datetime.time(9, 15)
            strategy.on_tick(_make_tick(symbol="2330", price=600.0, prev_close=580.0))
            strategy.on_tick(_make_tick(symbol="2330", price=610.0, prev_close=580.0))

        assert "2330" in strategy.positions
        strategy._virtual_fill_sell("2330", 610.0, 1, "trail")
        assert "2330" not in strategy.positions

        with patch("bot.strategy_configurable.now_tw_time") as mock_time:
            mock_time.return_value = datetime.time(11, 0)
            strategy.on_tick(_make_tick(symbol="2330", price=605.0, prev_close=580.0))

        buys = [s for s in strategy.signal_recorder._signals if s.action == "would-buy"]
        assert len(buys) == 2
        assert buys[1].reason == "reenter"


class TestTradeModeRegression:
    """trade 模式應繼續呼叫 broker 下單。"""

    def test_place_buy_calls_broker(self) -> None:
        settings = _make_settings(run_mode="trade")
        broker = MagicMock()
        broker.get_available_balance.return_value = 2_000_000.0
        mock_trade = MagicMock()
        mock_trade.order.ordno = "TEST001"
        broker.place_order.return_value = mock_trade

        strategy = ConfigurableStrategy(broker=broker, settings=settings)

        result = strategy._place_buy("2330", 600.0, 1, "enter")

        assert result is True
        broker.place_order.assert_called_once()
        assert broker.place_order.call_args.kwargs["custom_field"] == BOT_BUY_FIELD
        assert strategy.signal_recorder.signal_count == 0

    def test_place_sell_calls_broker_with_ai_sell_tag(self) -> None:
        settings = _make_settings(run_mode="trade")
        broker = MagicMock()
        mock_trade = MagicMock()
        mock_trade.order.ordno = "SELL001"
        broker.place_market_sell.return_value = mock_trade

        strategy = ConfigurableStrategy(broker=broker, settings=settings)
        strategy.positions["2330"] = PositionInfo(
            symbol="2330", avg_price=100.0, quantity=1,
        )
        strategy._last_price["2330"] = 110.0

        result = strategy._place_stop_sell("2330", 1, "target")

        assert result is True
        broker.place_market_sell.assert_called_once_with(
            "2330", 1, bot_sell_field("target"), max_sell_qty=1,
        )


class TestTradeRiskIntegration:
    def test_kill_switch_blocks_broker_buy(self, tmp_path: Path) -> None:
        settings = _make_settings(run_mode="trade")
        broker = MagicMock()
        broker.get_available_balance.return_value = 2_000_000.0
        strategy = ConfigurableStrategy(broker=broker, settings=settings)
        _bind_isolated_risk(strategy, settings, tmp_path)
        strategy.risk.engage_kill_switch("test")
        result = strategy._place_buy("2330", 600.0, 1, "enter")
        assert result is False
        broker.place_order.assert_not_called()

    def test_account_balance_unavailable_blocks_buy(self, tmp_path: Path) -> None:
        settings = _make_settings(run_mode="trade", check_account_balance=True)
        broker = MagicMock()
        broker.get_available_balance.return_value = None
        strategy = ConfigurableStrategy(broker=broker, settings=settings)
        _bind_isolated_risk(strategy, settings, tmp_path)
        result = strategy._place_buy("2330", 600.0, 1, "enter")
        assert result is False
        broker.place_order.assert_not_called()

    def test_odd_lot_trade_calls_place_odd_lot_order(self, tmp_path: Path) -> None:
        settings = _make_settings(
            run_mode="trade", use_odd_lot=True, max_fund=50_000,
        )
        broker = MagicMock()
        broker.get_available_balance.return_value = 100_000.0
        mock_trade = MagicMock()
        mock_trade.order.ordno = "ODD001"
        broker.place_odd_lot_order.return_value = mock_trade
        strategy = ConfigurableStrategy(broker=broker, settings=settings)
        _bind_isolated_risk(strategy, settings, tmp_path)
        qty, unit = strategy._calc_quantity(200.0)
        assert unit == "share"
        result = strategy._place_buy("2330", 200.0, qty, "enter", unit=unit)
        assert result is True
        broker.place_odd_lot_order.assert_called_once()
        broker.place_order.assert_not_called()

    def test_enter_placed_released_on_buy_cancel(self, tmp_path: Path) -> None:
        settings = _make_settings(run_mode="trade")
        broker = MagicMock()
        strategy = ConfigurableStrategy(broker=broker, settings=settings)
        _bind_isolated_risk(strategy, settings, tmp_path)
        strategy._enter_placed.add("2330")
        strategy.pending_orders["2330"] = ["ORD1"]
        strategy._order_meta["ORD1"] = {"symbol": "2330", "action": "Buy"}
        strategy.risk.reserve_entry_exposure("2330", 50_000)

        mock_trade = MagicMock()
        mock_trade.status.status.value = "Cancelled"
        mock_trade.order.ordno = "ORD1"
        mock_trade.order.action = "Buy"
        mock_trade.contract.code = "2330"
        broker.list_trades.return_value = [mock_trade]
        broker.update_status.return_value = None

        strategy._running = True
        calls = {"n": 0}

        def stop_after_update(_secs: float) -> None:
            calls["n"] += 1
            if calls["n"] >= 1:
                strategy._running = False

        with (
            patch("bot.strategy.now_tw_time", return_value=datetime.time(10, 0)),
            patch("bot.strategy.time.sleep", side_effect=stop_after_update),
        ):
            strategy._order_status_updater()

        assert "2330" not in strategy._enter_placed
        assert strategy.risk.effective_fund_remaining() == settings.max_fund

    def test_handle_deal_buy_and_sell(self, tmp_path: Path) -> None:
        settings = _make_settings(run_mode="trade")
        broker = MagicMock()
        strategy = ConfigurableStrategy(broker=broker, settings=settings)
        _bind_isolated_risk(strategy, settings, tmp_path)
        strategy.pending_orders["2330"] = ["B001"]
        strategy._entry_units["2330"] = "lot"

        strategy._handle_deal({
            "code": "2330",
            "action": "Buy",
            "price": 100.0,
            "quantity": 1,
            "ordno": "B001",
            "custom_field": BOT_BUY_FIELD,
        })
        assert "2330" in strategy.positions
        assert strategy.risk.fund_used == buy_cash_required(
            100.0, 1, "lot", settings=settings,
        )

        strategy._handle_deal({
            "code": "2330",
            "action": "Sell",
            "price": 96.0,
            "quantity": 1,
            "ordno": "S001",
            "custom_field": bot_sell_field("sl"),
        })
        assert "2330" not in strategy.positions
        assert strategy.risk.fund_used == 0.0

    def test_loss_exit_fund_used_zero(self, tmp_path: Path) -> None:
        settings = _make_settings(run_mode="watch")
        strategy = ConfigurableStrategy(broker=MagicMock(), settings=settings)
        _bind_isolated_risk(strategy, settings, tmp_path)
        strategy._virtual_fill_buy("2330", 100.0, 1, "enter")
        strategy._virtual_fill_sell("2330", 94.0, 1, "sl")
        assert strategy._fund_used == 0.0

    def test_closure_pending_cleared_on_sell_cancel(self, tmp_path: Path) -> None:
        settings = _make_settings(run_mode="trade")
        broker = MagicMock()
        strategy = ConfigurableStrategy(broker=broker, settings=settings)
        _bind_isolated_risk(strategy, settings, tmp_path)
        strategy._closure_pending.add("2330")
        strategy.pending_orders["2330"] = ["ORD2"]
        strategy._order_meta["ORD2"] = {
            "symbol": "2330", "action": "Sell", "custom_field": "close",
        }

        mock_trade = MagicMock()
        mock_trade.status.status.value = "Failed"
        mock_trade.order.ordno = "ORD2"
        mock_trade.order.action = "Sell"
        mock_trade.contract.code = "2330"
        broker.list_trades.return_value = [mock_trade]

        strategy._running = True

        def stop_sleep(_secs: float) -> None:
            strategy._running = False

        with (
            patch("bot.strategy.now_tw_time", return_value=datetime.time(10, 0)),
            patch("bot.strategy.time.sleep", side_effect=stop_sleep),
        ):
            strategy._order_status_updater()

        assert "2330" not in strategy._closure_pending
        assert "2330" not in strategy._closure_placed

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from shioaji.constant import OrderState

from bot.broker import SjBroker
from bot.config import Settings
from bot.events.bus import InMemoryEventBus
from bot.models import MarketTick, PositionInfo
from bot.paper_broker import PaperApi
from bot.recorder import TradeRecorder
from bot.simulation_check import _strategy, run_local
from bot.strategy_smile import SmileCurveStrategy
from bot.trade_cost import buy_cash_required
from bot.utils import now_tw


def setup(root, *, auto_fill=True, **overrides):
    params = dict(_env_file=None, api_key="local", secret_key="local", run_mode="trade",
                  simulation=True, strategy_type="smile_curve", symbols=["0050"],
                  symbols_auto_merge=False, max_fund=5000, daily_fund_budget=0,
                  use_odd_lot=True, smile_base_lot=10, odd_lot_max_shares=20,
                  min_price=0, max_price=0, telegram_bot_token="", telegram_chat_id="",
                  llm_gate_enabled=False, llm_sell_gate_enabled=False,
                  llm_intraday_review_enabled=False)
    params.update(overrides)
    settings = Settings(**params)
    broker = SjBroker(settings, simulation_only=True)
    api = PaperApi(settings, broker._dispatch_order_callback, auto_fill=auto_fill)
    broker.api = api
    broker._contracts["0050"] = api.contract("0050")
    return _strategy(settings, broker, root), api


def tick(strategy, price):
    with patch("bot.strategy_smile.now_tw_time", return_value=dt.time(10)):
        strategy.on_tick(MarketTick(now_tw(), "0050", price, 1, 0, 100, "local_paper"))


def update_once(strategy):
    strategy._running = True
    with patch("bot.strategy.time.sleep", side_effect=lambda _: setattr(strategy, "_running", False)):
        strategy._order_status_updater()


def test_all_strategies_finish_real_adapter_paper_cycles(tmp_path):
    result = run_local(tmp_path)
    assert result["result"] == "passed"
    assert len(result["cases"]) == 6
    assert all(case["final_positions"] == case["pending_orders"] == case["fund_used"] == 0
               for case in result["cases"])
    assert not list((tmp_path / "data").glob("trades_*.csv"))


def test_partial_fill_keeps_pending_reservation_and_share_unit(tmp_path):
    strategy, api = setup(tmp_path, auto_fill=False)
    tick(strategy, 100)
    tick(strategy, 98)
    trade = api.trades[0]
    assert strategy.engines["0050"].state.tiers_done == set()
    api.fill(trade, 4)
    assert strategy.positions["0050"].quantity == 4
    assert strategy.positions["0050"].unit == "share"
    assert strategy._has_pending("0050")
    assert strategy.risk.effective_fund_remaining() < 5000 - 980
    tick(strategy, 96)
    assert len(api.trades) == 1
    api.fill(trade, 6)
    assert strategy.positions["0050"].quantity == 10
    assert not strategy._has_pending("0050")
    assert strategy.engines["0050"].state.tiers_done == {0}


def test_duplicate_fill_never_creates_extra_inventory(tmp_path):
    strategy, api = setup(tmp_path)
    tick(strategy, 100)
    tick(strategy, 98)
    strategy._on_order_callback(OrderState.StockDeal, api.messages[0])
    assert strategy.positions["0050"].quantity == 10
    assert strategy.recorder.deal_count == 1


def test_cancel_unfilled_buy_can_retry_tier(tmp_path):
    strategy, api = setup(tmp_path, auto_fill=False)
    tick(strategy, 100)
    tick(strategy, 98)
    api.cancel_order(api.trades[0])
    update_once(strategy)
    assert not strategy._has_pending("0050")
    assert strategy.risk.effective_fund_remaining() == 5000
    assert not strategy.engines["0050"].state.tiers_done
    tick(strategy, 98)
    assert len(api.trades) == 2


def test_cancel_partially_filled_buy_releases_only_remaining_reservation(tmp_path):
    strategy, api = setup(tmp_path, auto_fill=False)
    tick(strategy, 100)
    tick(strategy, 98)
    api.fill(api.trades[0], 4)
    api.cancel_order(api.trades[0])
    update_once(strategy)
    assert not strategy._has_pending("0050")
    assert strategy.risk.effective_fund_remaining() == pytest.approx(
        5000 - buy_cash_required(98, 4, "share", settings=strategy.settings))
    assert strategy.positions["0050"].quantity == 4


def test_no_sale_at_rebound_if_fees_still_make_batch_unprofitable(tmp_path):
    strategy, api = setup(tmp_path, smile_base_lot=1)
    tick(strategy, 100)
    tick(strategy, 99)
    tick(strategy, 100)
    assert len(api.trades) == 1
    tick(strategy, 104)
    assert len(api.trades) == 2
    assert not strategy.positions


def test_smile_additions_respect_aggregate_position_cap(tmp_path):
    strategy, api = setup(tmp_path, max_lot_per_symbol=1, smile_base_lot=800,
                          odd_lot_max_shares=999, max_fund=500000)
    tick(strategy, 100)
    tick(strategy, 98)
    tick(strategy, 96)
    tick(strategy, 94)
    assert strategy.positions["0050"].quantity == 1000
    assert len(api.trades) == 2


def test_smile_state_and_records_restore_after_restart(tmp_path):
    strategy, api = setup(tmp_path)
    tick(strategy, 100)
    tick(strategy, 98)
    restored = _strategy(strategy.settings, strategy.broker, tmp_path)
    restored._run_startup_safety_and_restore()
    assert restored.positions["0050"].quantity == 10
    assert restored.positions["0050"].unit == "share"
    assert restored.engines["0050"].state.tiers_done == {0}
    tick(restored, 102)
    assert not restored.positions
    assert len(pd.read_csv(restored._state_root / "data" / f"trades_{now_tw().date()}.csv")) == 2


def test_smile_never_applies_day_trade_forced_close(tmp_path):
    strategy, _ = setup(tmp_path)
    tick(strategy, 100)
    tick(strategy, 98)
    strategy._running = True
    with patch("bot.strategy_smile.now_tw_time", return_value=dt.time(13, 30)):
        strategy._position_closure()
    assert strategy.positions["0050"].quantity == 10
    assert not strategy._running


def test_real_smile_rejected_before_strategy_initialization(tmp_path):
    settings = Settings(_env_file=None, run_mode="trade", simulation=False, api_key="local")
    with pytest.raises(ValueError, match="SIMULATION=true"):
        SmileCurveStrategy(None, settings, project_root=tmp_path)


def test_simulation_fence_blocks_changed_settings_or_production_api(tmp_path):
    strategy, api = setup(tmp_path)
    strategy.settings.simulation = False
    with pytest.raises(RuntimeError, match="production orders"):
        strategy.broker.place_odd_lot_order("0050", "Buy", 1, 100)
    with pytest.raises(RuntimeError, match="production login"):
        strategy.broker.login()
    assert not api.trades


def test_simulation_skips_unsupported_balance_but_enforces_budget(tmp_path):
    strategy, api = setup(tmp_path)
    with patch.object(strategy.broker, "get_available_balance", side_effect=AssertionError("No sim balance API")):
        tick(strategy, 100)
        tick(strategy, 98)
    assert strategy.positions["0050"].quantity == 10
    assert strategy.risk.fund_used <= 5000


def test_recorder_preserves_previous_session_and_exports_idempotently(tmp_path):
    first = TradeRecorder(str(tmp_path))
    first.record_deal(dict(code="0050", action="Buy", price=100, quantity=1, ordno="B1"), unit="share")
    target = first.export_csv()
    first.export_csv()
    second = TradeRecorder(str(tmp_path))
    second.record_deal(dict(code="0050", action="Sell", price=104, quantity=1, ordno="S1"), unit="share")
    second.export_csv()
    second.export_csv()
    assert list(pd.read_csv(target)["action"]) == ["Buy", "Sell"]


def test_main_dispatches_smile_and_blocks_real_before_login(tmp_path, monkeypatch):
    from bot import main
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, api_key="local", secret_key="local", run_mode="trade", simulation=True,
                        strategy_type="smile_curve", symbols=["0050"], symbols_auto_merge=False)
    with patch.object(main, "Settings", return_value=settings), \
         patch.object(main, "bootstrap_event_bus", return_value=InMemoryEventBus()), \
         patch.object(main, "_acquire_instance_lock"), patch.object(main, "_release_instance_lock"), \
         patch("bot.broker.SjBroker") as broker, \
         patch("bot.strategy_smile.SmileCurveStrategy") as smile, \
         patch("bot.strategy_configurable.ConfigurableStrategy") as configurable, \
         patch("bot.main.signal.signal"):
        main.main(["--root", str(tmp_path)])
        smile.assert_called_once()
        configurable.assert_not_called()
        broker.assert_called_once_with(settings, simulation_only=True)
        settings.simulation = False
        broker.reset_mock()
        with pytest.raises(SystemExit):
            main.main(["--root", str(tmp_path)])
        broker.assert_not_called()


def test_simulate_cli_overrides_production_env_in_memory(tmp_path, monkeypatch):
    from bot import main
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, api_key="local", secret_key="local", simulation=False,
                        symbols=["0050"], symbols_auto_merge=False)
    with patch.object(main, "Settings", return_value=settings), \
         patch.object(main, "bootstrap_event_bus", return_value=InMemoryEventBus()), \
         patch.object(main, "_acquire_instance_lock"), patch.object(main, "_release_instance_lock"), \
         patch("bot.broker.SjBroker") as broker, \
         patch("bot.strategy_smile.SmileCurveStrategy"), patch("bot.main.signal.signal"):
        main.main(["--root", str(tmp_path), "--simulate", "--strategy", "smile_curve"])
        effective = broker.call_args.args[0]
        assert effective.simulation is True and effective.run_mode == "trade"
        assert effective.use_odd_lot and effective.smile_base_lot == 100
        assert not settings.simulation and settings.run_mode == "watch"
        assert not effective.day_trading_unfreeze


def test_terminal_status_without_fill_callbacks_keeps_order_blocked(tmp_path):
    strategy, api = setup(tmp_path, auto_fill=False)
    tick(strategy, 100)
    tick(strategy, 98)
    from shioaji.constant import Status
    api.trades[0].status.status = Status.Cancelled
    api.trades[0].status.deal_quantity = 4
    update_once(strategy)
    assert strategy._has_pending("0050")
    assert strategy.engines["0050"].state.tiers_done == set()


@pytest.mark.parametrize("auto_fill", [True, False])
def test_shioaji_acceptance_requires_both_fills_and_cancels_timeout(tmp_path, monkeypatch, auto_fill):
    import bot.simulation_check as check
    (tmp_path / ".env").write_text("API_KEY=local\nSECRET_KEY=local\nSIMULATION=false\n", encoding="utf-8")
    created = []

    def factory(settings, **kwargs):
        assert settings.simulation is True and kwargs["simulation_only"] is True
        broker = SjBroker(settings, **kwargs)
        api = PaperApi(settings, broker._dispatch_order_callback, auto_fill=auto_fill)
        broker.api = api
        broker._contracts["0050"] = api.contract("0050")
        broker.login = lambda: True
        created.append(api)
        return broker

    monkeypatch.setattr(check, "SjBroker", factory)
    if not auto_fill:
        monkeypatch.setattr(check.time, "monotonic", MagicMock(side_effect=[0, 2]))
    result = check.run_shioaji(tmp_path / "output", tmp_path, 1)
    assert result["final_test_position_shares"] == 0
    assert result["production_orders"] == 0
    if auto_fill:
        assert result["result"] == "passed"
        assert result["fill_records"] == 2
    else:
        assert result["result"] == "blocked"
        assert len(created[0].trades) == 1
        assert result["cleanup"][0]["status"] == "Cancelled"


def test_startup_failure_logs_out_and_releases_instance_lock(tmp_path, monkeypatch):
    from bot import main
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, api_key="local", secret_key="local", run_mode="trade",
                        simulation=True, strategy_type="smile_curve", symbols=["0050"],
                        symbols_auto_merge=False)
    with patch.object(main, "Settings", return_value=settings), \
         patch.object(main, "bootstrap_event_bus", return_value=InMemoryEventBus()), \
         patch.object(main, "_acquire_instance_lock"), patch.object(main, "_release_instance_lock") as release, \
         patch("bot.broker.SjBroker") as broker, \
         patch("bot.strategy_smile.SmileCurveStrategy", side_effect=ValueError("bad settings")):
        with pytest.raises(ValueError, match="bad settings"):
            main.main(["--root", str(tmp_path)])
        broker.return_value.logout.assert_called_once()
        release.assert_called_once()

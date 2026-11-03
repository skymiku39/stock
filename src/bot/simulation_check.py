"""Bounded simulation acceptance. Never constructs a production connection.

uv run python -m bot.simulation_check
uv run python -m bot.simulation_check --backend shioaji --timeout 60
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import time
from unittest.mock import patch
from uuid import uuid4

from shioaji.constant import Unit

from bot.broker import SjBroker
from bot.cli_encoding import configure_console
from bot.config import Settings
from bot.events.bus import InMemoryEventBus
from bot.events.trading_handlers import TradeRecordingHandler
from bot.events.types import TradeBuyFilled, TradeSellFilled
from bot.models import MarketTick
from bot.paper_broker import PaperApi
from bot.strategy_configurable import ConfigurableStrategy
from bot.strategy_smile import SmileCurveStrategy
from bot.utils import now_tw


def _strategy(settings, broker, root):
    bus = InMemoryEventBus()
    cls = SmileCurveStrategy if settings.strategy_type == "smile_curve" else ConfigurableStrategy
    strategy = cls(broker=broker, settings=settings, publisher=bus,
                   project_root=root, wire_handlers=False)
    handler = TradeRecordingHandler(strategy.recorder)
    bus.subscribe(TradeBuyFilled, handler)
    bus.subscribe(TradeSellFilled, handler)
    broker.set_on_order(strategy._on_order_callback)
    return strategy


def run_local(root: Path) -> dict:
    cases = []
    for kind in ("configurable", "smile_curve"):
        for symbol, unit in (("2330", "share"), ("0050", "share"), ("0050", "lot")):
            settings = Settings(
                _env_file=None, api_key="local-only", secret_key="local-only",
                run_mode="trade", simulation=True, strategy_type=kind,
                symbols=[symbol], symbols_auto_merge=False, daily_fund_budget=0,
                max_fund=500000 if symbol == "2330" or unit == "lot" else 5000,
                use_odd_lot=unit == "share", odd_lot_max_shares=10,
                smile_base_lot=10 if unit == "share" else 1,
                max_lot_per_symbol=2, min_price=0, max_price=0,
                per_order_max_cost_twd=0, daily_max_orders=0,
                per_symbol_daily_max_orders=0, max_open_positions=0,
                blacklist_symbols=[], manual_hold_symbols=[],
                telegram_bot_token="", telegram_chat_id="",
                llm_gate_enabled=False, llm_sell_gate_enabled=False,
                llm_intraday_review_enabled=False, check_account_balance=True,
            )
            broker = SjBroker(settings, simulation_only=True)
            api = PaperApi(settings, broker._dispatch_order_callback)
            broker.api = api
            broker._contracts[symbol] = api.contract(symbol)
            strategy = _strategy(settings, broker, root / f"{kind}-{symbol}-{unit}")
            base = 2300.0 if symbol == "2330" else 100.0
            prices = ([base, base * .98, base * 1.02] if kind == "smile_curve"
                      else [base, base * 1.02, base * .96])
            # A deterministic exchange clock applies only to the local matcher.
            with patch("bot.strategy.now_tw_time", return_value=dt.time(10)), \
                 patch("bot.strategy_configurable.now_tw_time", return_value=dt.time(10)), \
                 patch("bot.strategy_smile.now_tw_time", return_value=dt.time(10)):
                for price in prices:
                    api.prices[symbol] = price
                    strategy.on_tick(MarketTick(now_tw(), symbol, price, 1,
                                               (price / base - 1) * 100, base, "local_paper"))
            rows = list(strategy.recorder._records)
            passed = (len(rows) == 2 and [r["action"] for r in rows] == ["Buy", "Sell"]
                      and not strategy.positions and not any(strategy.pending_orders.values())
                      and not api.list_positions() and strategy.risk.fund_used == 0)
            if not passed:
                raise AssertionError(f"Incomplete simulated cycle: {kind} {symbol}")
            strategy.recorder.export_csv()
            cases.append(dict(strategy=kind, symbol=symbol, quantity_unit=unit, result="passed",
                              fills=[dict(side=r["action"], price=r["price"], quantity=r["quantity"],
                                          unit=r["unit"]) for r in rows],
                              final_positions=0, pending_orders=0, fund_used=0,
                              final_paper_cash=api.cash))
    return dict(backend="local_paper", simulation=True, result="passed", cases=cases,
                limit="Deterministic matching validates application flow, not broker execution")


def run_shioaji(root: Path, source_root: Path, timeout: int) -> dict:
    # Constructor arguments override .env and environment variables. There is
    # deliberately no CLI switch capable of selecting production.
    settings = Settings(
        _env_file=str(source_root / ".env"), run_mode="trade", simulation=True,
        strategy_type="configurable", symbols=["0050"], symbols_auto_merge=False,
        use_odd_lot=False, max_fund=500000, daily_fund_budget=0,
        telegram_bot_token="", telegram_chat_id="", llm_gate_enabled=False,
        llm_sell_gate_enabled=False, llm_intraday_review_enabled=False,
    )
    broker = SjBroker(settings, simulation_only=True)
    result = dict(backend="shioaji", simulation=True, symbol="0050", result="blocked",
                  phases=[], production_orders=0)
    strategy = None
    submitted = []
    try:
        if not broker.login():
            result["reason"] = "Simulation login failed"
            return result
        assert broker.api.simulation is True
        baseline = broker.api.list_positions(broker.api.stock_account, unit=Unit.Share)
        if any(p.code == "0050" and p.quantity for p in baseline):
            result["reason"] = "Existing simulation position; refused to mix test inventory"
            return result
        strategy = _strategy(settings, broker, root)
        contract = broker.get_contract("0050")
        if contract is None:
            result["reason"] = "0050 contract missing"
            return result
        strategy._last_price["0050"] = float(contract.reference)
        for side in ("Buy", "Sell"):
            assert settings.simulation and broker.api.simulation is True
            placed = (strategy._place_buy("0050", float(contract.limit_up), 1, "simulation_check")
                      if side == "Buy" else strategy._place_stop_sell("0050", 1, "simulation_check"))
            if not placed:
                result["reason"] = f"{side} order refused"
                return result
            # Scope all cleanup to orders created by this strategy instance.
            trade = broker.last_submitted_trade
            if trade is None:
                result["reason"] = "Submitted order unavailable for tracking"
                return result
            submitted.append(trade)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                broker.update_status(trade)
                strategy._drain_order_events()
                status = trade.status.status.value
                filled_locally = ("0050" in strategy.positions) if side == "Buy" else not strategy.positions
                if status == "Filled" and filled_locally and not strategy._has_pending("0050"):
                    break
                if status in ("Failed", "Cancelled"):
                    break
                time.sleep(1)
            phase = dict(side=side, status=trade.status.status.value,
                         deal_quantity=trade.status.deal_quantity,
                         status_code=trade.status.status_code, message=trade.status.msg)
            result["phases"].append(phase)
            if phase["status"] != "Filled" or strategy._has_pending("0050"):
                result["reason"] = "Broker fill and application fill callbacks have not both completed"
                return result
        positions = broker.api.list_positions(broker.api.stock_account, unit=Unit.Share)
        result["final_test_position_shares"] = sum(p.quantity for p in positions if p.code == "0050")
        result["fill_records"] = strategy.recorder.deal_count
        if not result["final_test_position_shares"] and not strategy.positions and strategy.recorder.deal_count >= 2:
            result["result"] = "passed"
        return result
    except Exception as exc:
        result["result"] = "failed"
        result["reason"] = f"Simulation check failed: {type(exc).__name__}"
        return result
    finally:
        for trade in submitted:
            if trade.status.status.value not in ("Filled", "Cancelled", "Failed"):
                try:
                    broker.cancel_order(trade)
                except Exception:
                    result["result"] = "failed"
                result.setdefault("cleanup", []).append(dict(status=trade.status.status.value))
        if strategy is not None:
            strategy.recorder.export_csv()
            result["application_open_positions"] = list(strategy.positions)
        if broker.api is not None:
            try:
                positions = broker.api.list_positions(broker.api.stock_account, unit=Unit.Share)
                result["final_test_position_shares"] = sum(p.quantity for p in positions if p.code == "0050")
            except Exception:
                result["result"] = "failed"
                result["final_test_position_shares"] = "unknown"
            finally:
                broker.logout()


def main(argv=None):
    configure_console()
    parser = argparse.ArgumentParser(description="Simulation-only buy/sell acceptance")
    parser.add_argument("--backend", choices=("local", "shioaji"), default="local")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    output = args.output or args.root / "work" / ("simulation-" + now_tw().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=True)
    result = (run_local(output) if args.backend == "local" else
              run_shioaji(output, args.root.resolve(), args.timeout))
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Execution timing and portfolio accounting regressions, entirely offline."""
import math

import pytest

from bot.config import Settings
from bot.smile_execution import SmileExecution
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.stock_db import PriceBar


def bars(symbol, prices):
    return [PriceBar(symbol, f"2026-01-{i+1:02d}", open=o, high=max(o,c)+20,
                     low=min(o,c)-20, close=c, volume=100000) for i, (o,c) in enumerate(prices)]


def run(histories, **overrides):
    values = dict(_env_file=None, max_fund=500000, daily_fund_budget=0,
                  smile_buy_tiers="1:1,50:1", smile_base_lot=1,
                  max_lot_per_symbol=1, broker_fee_discount=0, broker_min_fee=0,
                  smile_regular_tax_rate=0)
    values.update(overrides)
    return SmileCurveBacktester(Settings(**values), execution=SmileExecution.ideal()).run_histories(histories)


def test_signal_then_next_open_and_no_extreme_fills():
    r = run({"A": bars("A", [(100,100),(100,98),(95,102),(104,104)])})
    t, = r.all_round_trips
    assert (t.entry_date,t.entry_price,t.exit_date,t.exit_price) == (
        "2026-01-03",95,"2026-01-04",104)
    assert r.net_pnl_twd == 9000
    assert all(o["signal_date"] < o["execution_date"] for o in r.orders)


def test_last_close_signal_is_not_filled():
    r = run({"A": bars("A", [(100,100),(100,98)])})
    assert r.orders == []
    assert r.cash == 500000
    assert r.results[0].pending_orders == 1


def test_gap_down_sell_cancelled_and_unrealized_loss_in_drawdown():
    r = run({"A": bars("A", [(100,100),(100,98),(99,101),(70,70)])})
    assert r.total_pnl_twd == 0
    assert r.unrealized_pnl_twd == -29000
    assert r.net_pnl_twd == -29000
    assert r.max_drawdown_twd == 31000
    assert r.max_drawdown_pct == pytest.approx(31000/502000*100)
    assert r.orders[-1]["reason"] == "unprofitable_open"
    assert r.equity_curve[-1]["equity"] == 471000


def test_cash_shared_and_symbol_order_deterministic():
    h = {s: bars(s, [(100,100),(100,98),(99,99)]) for s in ["B","A"]}
    r = run(h, max_fund=100000)
    assert r.cash == 1000
    assert [s.open_lots for s in r.results] == [1,0]
    assert run(dict(reversed(list(h.items()))), max_fund=100000).to_dict() == r.to_dict()
    assert all(p["cash"] >= 0 and p["cost_exposure"] <= 100000 for p in r.equity_curve)


def test_sales_do_not_fund_simultaneous_open_buys():
    r = run({"A":bars("A",[(100,100),(100,98),(90,101),(105,105)]),
             "B":bars("B",[(100,100),(100,100),(100,98),(90,90)])}, max_fund=100000)
    assert r.total_pnl_twd == 15000
    assert r.results[1].open_lots == 0
    assert r.cash == 115000


def test_missing_session_waits_and_invalid_open_cancels():
    a = bars("A",[(100,100),(100,98),(math.nan,98),(95,95)])
    r = run({"A":a})
    assert r.orders[0]["reason"] == "invalid_open"
    assert r.results[0].open_lots == 1
    a[2].date = "2026-01-05"
    a[3].date = "2026-01-06"
    r = run({"A":a})
    assert r.orders[0]["execution_date"] == "2026-01-05"


def test_fee_accounting_identity_and_stale_marks():
    r = run({"A":bars("A",[(100,100),(100,98),(95,96)]),
             "B":bars("B",[(100,100)]*4)},
            broker_fee_discount=0.28, broker_min_fee=1, smile_regular_tax_rate=0.003)
    assert r.equity_curve[-1]["stale_marks"] == ["A"]
    assert r.equity_curve[-1]["equity"] == pytest.approx(r.initial_cash+r.net_pnl_twd)
    assert r.cash+r.results[0].open_cost_twd == pytest.approx(r.initial_cash)


def test_odd_lot_position_cap_uses_share_equivalent():
    r = run({"A":bars("A",[(100,100),(100,98),(95,95),(95,95)])},
            use_odd_lot=True, smile_base_lot=1500)
    filled = [o for o in r.orders if o["status"] == "filled"]
    assert sum(o["filled_quantity"] for o in filled) == 1000


def test_same_day_high_low_never_produces_round_trip():
    r = run({"A":bars("A",[(100,100),(100,92)])})
    assert r.all_round_trips == []
    assert r.net_pnl_twd == 0


def test_partial_fill_does_not_overspend_and_no_data_is_reported():
    r = run({"A":bars("A",[(100,100),(100,98),(90,90)]),"B":[]},
            max_fund=95000, smile_base_lot=5, max_lot_per_symbol=10)
    assert r.cash == 5000
    assert r.orders[0]["filled_quantity"] == 1
    assert r.results[1].skip_reason == "no_daily_bars"


def test_audit_default_shares_capital_and_exports_metrics(tmp_path):
    from bot.stock_db import StockDB
    from bot.smile_curve_backtest_analysis import BacktestScenario, audit_symbols, DEFAULT_SCENARIOS
    assert all(s.price_mode == "next_open" for s in DEFAULT_SCENARIOS)
    with_db = StockDB.open(path=tmp_path / "test.db")
    try:
        for s in ("A", "B"):
            with_db.bulk_upsert_price_bars(bars(s, [(100,100),(100,98),(99,95)]))
        scenario = BacktestScenario("shared","2026-01-01","2026-01-03",100000,False,"1:1,50:1",1)
        reports = audit_symbols(with_db,["A","B"],[scenario])
        assert sum(r.open_lots for r in reports) == 1
        assert reports[0].portfolio == reports[1].portfolio
        assert reports[0].portfolio["max_drawdown_pct"] > 0
        assert sum(r.to_dict()["net_pnl_twd"] for r in reports) == pytest.approx(
            reports[0].portfolio["net_pnl_twd"])
    finally:
        with_db.close()


def test_future_changes_cannot_change_prior_fills():
    a = bars("A", [(100,100),(100,98),(95,96),(90,90)])
    first = run({"A":a})
    a[-1].open = 300
    a[-1].close = 300
    second = run({"A":a})
    assert first.orders[0] == second.orders[0]
    assert first.equity_curve[:3] == second.equity_curve[:3]


def test_cli_json_uses_explicit_cap_and_contains_equity(tmp_path, capsys):
    import json
    from bot.stock_db import StockDB, default_db_path
    from bot.smile_curve_backtest_cli import main
    (tmp_path / ".env").write_text("DAILY_FUND_BUDGET=1\n", encoding="utf-8")
    db = StockDB.open(path=default_db_path(tmp_path))
    try:
        db.bulk_upsert_price_bars(bars("TEST", [(100,100),(100,98),(95,95)]))
    finally:
        db.close()
    assert main(["--root",str(tmp_path),"--symbols","TEST","--start","2026-01-01",
                 "--end","2026-01-03","--max-fund","100000","--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["initial_cash"] == 100000
    assert payload["execution_mode"] == "close_signal_next_open"
    assert len(payload["equity_curve"]) == 3
    assert payload["unrealized_pnl_twd"] < 0
    assert payload["net_pnl_twd"] == payload["realized_pnl_twd"] + payload["unrealized_pnl_twd"]

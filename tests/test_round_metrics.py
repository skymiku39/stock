"""Round-level snowball metrics (output only)."""
from types import SimpleNamespace

import pytest

from bot.config import Settings
from bot.round_metrics import Round, account_metrics, group_rounds, summarize_rounds
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.smile_execution import SmileExecution
from bot.stock_db import PriceBar


def trip(sym, entry, exit_, price, qty, pnl, reason):
    return SimpleNamespace(symbol=sym, entry_date=entry, exit_date=exit_, entry_price=price,
                           quantity=qty, unit="lot", exit_reason=reason, pnl_twd=pnl)


def cost(t):
    return t.entry_price * t.quantity * 1000


def rnd(pnl, cost_twd, hold, reason="take_profit"):
    return Round("A", "2026-01-01", "2026-01-02", 1, cost_twd, pnl, 100 * pnl / cost_twd, hold, reason)


def test_group_multiple_lots_into_one_round():
    trips = [trip("A", "2026-01-05 09:00:00", "2026-03-10", 100, 1, 3000, "close_signal_next_open"),
             trip("A", "2026-01-02", "2026-03-10", 90, 2, 1000, "close_signal_next_open"),
             trip("B", "2026-02-01", "2026-02-11", 50, 1, -500, "stop_loss")]
    rounds = group_rounds(trips, cost)
    assert [(r.symbol, r.exit_date) for r in rounds] == [("B", "2026-02-11"), ("A", "2026-03-10")]
    b, a = rounds
    assert (a.lot_count, a.cost_twd, a.pnl_twd, a.entry_date) == (2, 280000, 4000, "2026-01-02")
    assert a.hold_days == 67 and a.exit_reason == "take_profit"
    assert a.return_pct == pytest.approx(100 * 4000 / 280000)
    assert b.exit_reason == "stop_loss" and b.hold_days == 10


def test_reason_mapping():
    ts = [trip("A", "2026-01-01", f"2026-01-0{i+2}", 10, 1, 1, r)
          for i, r in enumerate(["rebound", "stop_loss_next_open", "weird"])]
    assert [r.exit_reason for r in group_rounds(ts, cost)] == ["take_profit", "stop_loss", "weird"]


def test_summarize_mixed():
    rs = [rnd(1000, 10000, 10), rnd(3000, 10000, 30), rnd(-500, 10000, 100, "stop_loss")]
    m = summarize_rounds(rs)
    assert m["round_count"] == 3 and m["win_count"] == 2
    assert m["win_rate"] == pytest.approx(200 / 3)
    assert m["avg_win_twd"] == 2000 and m["avg_loss_twd"] == -500
    assert m["profit_factor"] == pytest.approx(8.0)
    assert m["expectancy_twd"] == pytest.approx(1166.6666667)
    assert m["expectancy_pct"] == pytest.approx((10 + 30 - 5) / 3)
    assert (m["hold_days_median"], m["hold_days_max"]) == (30, 100)
    assert m["hold_days_mean"] == pytest.approx(140 / 3)
    assert m["over_90_days_pct"] == pytest.approx(100 / 3)
    assert m["exit_reason_counts"] == {"take_profit": 2, "stop_loss": 1}


def test_no_losses_profit_factor_none_and_empty():
    assert summarize_rounds([rnd(10, 100, 1)])["profit_factor"] is None
    m = summarize_rounds([])
    assert m["round_count"] == 0 and m["profit_factor"] is None and m["exit_reason_counts"] == {}
    assert group_rounds([], cost) == []
    a = account_metrics([], 1000, 0)
    assert a["annualized_return_pct"] == 0 and a["annual_turnover"] is None


def test_account_metrics_two_points():
    curve = [dict(date="2025-01-01", equity=100.0, cost_exposure=0.0),
             dict(date="2026-01-01", equity=121.0, cost_exposure=60.5)]
    a = account_metrics(curve, 100.0, 200.0)
    assert a["annualized_return_pct"] == pytest.approx(21.0)
    assert a["max_drawdown_pct"] == 0 and a["return_over_mdd"] is None
    assert a["avg_capital_utilization_pct"] == pytest.approx(25.0)
    assert a["annual_turnover"] == pytest.approx(200 / 110.5 / 1.0)
    one = account_metrics(curve[:1], 100.0, 10.0)
    assert one["annualized_return_pct"] == 0 and one["annual_turnover"] is None


def test_integration_to_dict_contains_round_metrics():
    prices = [(100, 100), (100, 98), (95, 102), (104, 104)]
    bars = [PriceBar("A", f"2026-01-{i+1:02d}", open=o, high=max(o, c) + 20, low=min(o, c) - 20,
                     close=c, volume=100000) for i, (o, c) in enumerate(prices)]
    s = Settings(_env_file=None, max_fund=500000, daily_fund_budget=0, smile_buy_tiers="1:1,50:1",
                 smile_base_lot=1, max_lot_per_symbol=1, broker_fee_discount=0, broker_min_fee=0,
                 smile_regular_tax_rate=0)
    r = SmileCurveBacktester(s, execution=SmileExecution.ideal()).run_histories({"A": bars})
    d = r.to_dict()
    assert len(d["rounds"]) == 1 and d["round_metrics"]["round_count"] == 1
    assert sum(x["pnl_twd"] for x in d["rounds"]) == pytest.approx(d["realized_pnl_twd"])
    assert d["account_metrics"]["max_drawdown_pct"] >= 0
    assert d["buy_cost_total_twd"] == 95000
    assert d["open_positions"] == dict(count=0, unrealized_pnl_twd=0.0)
    assert d["symbols"][0]["round_count"] == 1

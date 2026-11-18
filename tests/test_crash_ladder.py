import datetime as dt

import pytest

from bot.config import Settings
from bot.crash_ladder import CrashLadderBacktester, CrashLadderParams, crash_entry_rate
from bot.random_entry import RandomEntry
from bot.smile_execution import SmileExecution
from bot.stock_db import PriceBar

START = dt.date(2026, 1, 1)
SETTINGS = dict(_env_file=None, broker_fee_discount=0, broker_min_fee=0)


def bars(symbol, closes, volume=1_000_000):
    """開盤價 = 前一日收盤價。"""
    opens = [closes[0]] + list(closes[:-1])
    return [PriceBar(symbol, (START + dt.timedelta(days=i)).isoformat(), open=o, high=max(o, c),
                     low=min(o, c), close=c, volume=volume) for i, (o, c) in enumerate(zip(opens, closes))]


# 兩檔同步：100 → 跌到 88（−12%）→ 84（−16%）→ 回升。
PATH = [100, 100, 95, 88, 86, 84, 90, 95, 100, 102, 104, 106, 108, 110]


def run(histories, **params):
    bt = CrashLadderBacktester(Settings(**SETTINGS), CrashLadderParams(**params),
                               execution=SmileExecution.ideal(), initial_cash=300_000)
    return bt.run_histories(histories)


def test_ladder_buys_one_tranche_per_new_level_and_exits_after_hold():
    h = {"A": bars("A", PATH), "B": bars("B", PATH)}
    r = run(h, first_drop_pct=10, step_pct=5, tranches=3, max_hold_days=3)
    buys = [o for o in r.orders if o["side"] == "buy"]
    # 第 4 天收 88 跨過 10% → 第 5 天開盤（88）買；第 6 天收 84 跨過 15% → 第 7 天開盤（84）買。
    assert [(o["signal_date"], o["execution_date"], o["status"]) for o in buys] == [
        ("2026-01-04", "2026-01-05", "filled"), ("2026-01-06", "2026-01-07", "filled")]
    assert all(o["amount"] == pytest.approx(100_000) for o in buys)  # 權益 ÷ 3 批
    assert len(r.rounds) == 2 and all(rd.exit_reason == "expiry" for rd in r.rounds)
    first = r.rounds[0]
    # 01-05 以 88 買、持有滿 3 天（01-08 收盤）→ 01-09 開盤（＝01-08 收盤 95）賣。
    assert (first.entry_date, first.exit_date) == ("2026-01-05", "2026-01-09")
    assert first.return_pct == pytest.approx((95 * (1 - 0.003) / 88 - 1) * 100, abs=0.1)
    assert r.open_positions == []


def test_crossing_two_levels_in_one_day_buys_double_and_new_high_resets():
    path = [100, 100, 84, 84, 84, 101, 101, 89, 89, 89, 89]
    r = run({"A": bars("A", path)}, first_drop_pct=10, step_pct=5, tranches=3, max_hold_days=2)
    buys = [o for o in r.orders if o["side"] == "buy" and o["status"] == "filled"]
    assert buys[0]["amount"] == pytest.approx(200_000)       # 一天跌 16%，跨兩階
    assert len(buys) == 2 and buys[1]["signal_date"] == "2026-01-08"  # 新高後階數歸零，再跌 10% 又買


def test_no_signal_before_trade_start_but_peak_is_tracked():
    h = {"A": bars("A", PATH)}
    bt = CrashLadderBacktester(Settings(**SETTINGS), CrashLadderParams(first_drop_pct=10, max_hold_days=3),
                               execution=SmileExecution.ideal(), initial_cash=300_000)
    r = bt.run_histories(h, trade_start="2026-01-06")
    buys = [o for o in r.orders if o["side"] == "buy"]
    assert [o["signal_date"] for o in buys] == ["2026-01-06"]  # 前高 100 來自暖機期，16% 一次跨兩階
    assert r.equity_curve[0]["date"] == "2026-01-06"


def test_ex_rights_pays_cash_to_holdings():
    h = {"A": bars("A", PATH)}
    events = {"A": [dict(ex_date="2026-01-06", prev_close=86, reference_price=84)]}
    bt = CrashLadderBacktester(Settings(**SETTINGS), CrashLadderParams(first_drop_pct=10, max_hold_days=3),
                               execution=SmileExecution.ideal(), initial_cash=300_000)
    r = bt.run_histories(h, ex_rights=events)
    shares = 100_000 // 88
    assert r.distribution_total_twd == pytest.approx(2 * shares)


def test_random_entry_is_reproducible_and_rate_helper():
    h = {"A": bars("A", [100 + i for i in range(40)]), "B": bars("B", [100 - i * 0.2 for i in range(40)])}
    bt = CrashLadderBacktester(Settings(**SETTINGS), CrashLadderParams(max_hold_days=5),
                               execution=SmileExecution.ideal(), initial_cash=300_000)
    a = bt.run_histories(h, random_entry=RandomEntry({"entry": 0.3}, "x"))
    b = bt.run_histories(h, random_entry=RandomEntry({"entry": 0.3}, "x"))
    assert a.orders == b.orders and a.orders
    assert all(o["label"] == "random" for o in a.orders if o["side"] == "buy")
    assert 0 < crash_entry_rate(a)["entry"] <= 1


def test_data_end_sells_at_last_close():
    h = {"A": bars("A", PATH), "B": bars("B", PATH[:6])}
    bt = CrashLadderBacktester(Settings(**SETTINGS), CrashLadderParams(first_drop_pct=10, max_hold_days=30),
                               execution=SmileExecution.ideal(), initial_cash=300_000, data_end_sessions=2)
    r = bt.run_histories(h)
    assert r.data_ended[0]["symbol"] == "B" and r.data_ended[0]["forced_exit_date"] == "2026-01-07"


def test_params_validation():
    for bad in (dict(first_drop_pct=0), dict(step_pct=-1), dict(tranches=0), dict(max_hold_days=1.5)):
        with pytest.raises(ValueError):
            CrashLadderParams(**bad)
    assert CrashLadderParams(first_drop_pct=15, step_pct=5, tranches=3).levels == [15, 20, 25]


def test_compare_crash_train_only_grid_and_render():
    from bot.crash_ladder_strategy import CRASH_AXES, compare_crash, render
    long_path = PATH + [110 - i for i in range(30)] + [80 + i for i in range(30)]
    h = {"A": bars("A", long_path), "B": bars("B", long_path)}
    result = compare_crash(dict(train=(h, {}), warm=(h, {})), validate=False)
    combos = result["strategies"]["crash"]["combos"]
    assert len(combos) == len(CRASH_AXES[0][1]) * len(CRASH_AXES[1][1])
    assert sum(c["chosen"] for c in combos) == 1
    assert "大跌分批加碼" in render(result)


@pytest.mark.parametrize("weighting,amounts", [("equal", [100_000, 100_000]), ("front", [150_000, 75_000]),
                                               ("all_in", [300_000])])
def test_weighting_sets_tranche_amounts(weighting, amounts):
    h = {"A": bars("A", PATH), "B": bars("B", PATH)}
    r = run(h, first_drop_pct=10, step_pct=5, tranches=3, max_hold_days=30, weighting=weighting)
    buys = [o for o in r.orders if o["side"] == "buy"]
    assert [o["amount"] for o in buys] == pytest.approx(amounts)


def test_weights_and_validation():
    assert CrashLadderParams(weighting="front").weights == [0.5, 0.25, 0.25]
    assert CrashLadderParams(weighting="all_in").weights == [1.0, 0.0, 0.0]
    with pytest.raises(ValueError):
        CrashLadderParams(weighting="double")

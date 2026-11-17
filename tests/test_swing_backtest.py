"""波段策略回測原型（#19）：進場、停損、移動停利、期限、部位大小、除權息與資料中斷。"""
import datetime as dt

import pytest

from bot.config import Settings
from bot.random_entry import RandomEntry, swing_random_baseline
from bot.smile_execution import SmileExecution
from bot.stock_db import PriceBar
from bot.swing_backtest import SwingBacktester, SwingParams

START = dt.date(2026, 1, 1)
SETTINGS = dict(_env_file=None, broker_fee_discount=0, broker_min_fee=0)
SMALL = dict(breakout_days=3, fast_ma=2, slow_ma=4, atr_days=3, momentum_days=3)


def bars(symbol, closes, *, spread=1.0, volume=100000):
    """開盤價 = 前一日收盤價；高低點 = 開收盤的極值 ± spread。"""
    opens = [closes[0]] + list(closes[:-1])
    return [PriceBar(symbol, (START + dt.timedelta(days=i)).isoformat(), open=o,
                     high=max(o, c) + spread, low=min(o, c) - spread, close=c, volume=volume)
            for i, (o, c) in enumerate(zip(opens, closes))]


def run(histories, *, initial_cash=1000000, execution=None, revenue=None, **params):
    backtester = SwingBacktester(Settings(**SETTINGS), SwingParams(**{**SMALL, **params}),
                                 execution=execution or SmileExecution.ideal(),
                                 initial_cash=initial_cash)
    return backtester.run_histories(histories, revenue=revenue)


def fills(result, side):
    return [(o["execution_date"], o["execution_price"], o["filled_quantity"])
            for o in result.orders if o["side"] == side and o["status"] == "filled"]


FLAT = [100, 100, 100, 100, 100]


def test_breakout_entry_sizes_by_risk_and_exits_on_trailing_stop():
    # 第 6 根收 104 突破前 3 日最高收盤 100，短均線 102 > 長均線 101 → 次日開盤 104 買進。
    closes = FLAT + [104, 108, 112, 116, 96, 96]
    r = run({"A": bars("A", closes)})
    (date, price, qty), = fills(r, "buy")
    assert (date, price) == ("2026-01-07", 104)
    # 訊號日 ATR(3)：三根的真實波幅 2、2、6 → 10/3；股數 = 1,000,000 × 1% ÷ (2 × ATR)，向下取整。
    assert qty == 1499
    buy = next(o for o in r.orders if o["side"] == "buy")
    assert buy["stop"] == pytest.approx(104 - 2 * 10 / 3)
    # 移動停利已上移到 116 − 3 × 6 = 98；收盤 96 跌破 → 次日開盤 96 賣出。
    (sell_date, sell_price, _), = fills(r, "sell")
    assert (sell_date, sell_price) == ("2026-01-11", 96)
    rnd, = r.rounds
    pnl = (96 - 104) * 1499 - 96 * 1499 * 0.003  # 價差與證交稅
    assert rnd.exit_reason == "trailing_stop" and rnd.pnl_twd == pytest.approx(pnl)
    assert r.cash == pytest.approx(1000000 + pnl) and r.open_positions == []


def test_no_entry_without_uptrend_or_before_trade_start():
    falling = [100, 99, 98, 97, 96, 95, 96.5, 95, 94]  # 有一根反彈但均線仍空頭排列
    assert run({"A": bars("A", falling)}).orders == []
    closes = FLAT + [104, 108, 112, 116, 120]
    backtester = SwingBacktester(Settings(**SETTINGS), SwingParams(**SMALL),
                                 execution=SmileExecution.ideal(), initial_cash=1000000)
    late = backtester.run_histories({"A": bars("A", closes)}, trade_start="2026-01-08")
    assert fills(late, "buy")[0][0] == "2026-01-09"  # 01-08 之前的突破只用來暖機


def test_initial_stop_and_expiry_exits():
    stopped = run({"A": bars("A", FLAT + [104, 104, 95, 95])})
    assert stopped.rounds[0].exit_reason == "stop_loss"
    assert fills(stopped, "sell")[0][:2] == ("2026-01-09", 95)
    drifting = run({"A": bars("A", FLAT + [104] + [105] * 8)}, max_hold_days=4, trail_atr=50)
    rnd, = drifting.rounds
    assert rnd.exit_reason == "expiry" and rnd.entry_date == "2026-01-07"
    assert rnd.exit_date == "2026-01-12" and rnd.hold_days == 5  # 滿 4 天收盤發訊號，次日成交


def test_position_cap_slot_limit_and_momentum_ranking():
    capped = run({"A": bars("A", FLAT + [104, 108, 112], spread=0.01)})
    qty = fills(capped, "buy")[0][2]
    assert qty == int(1000000 * 0.20 // 104)  # 風險算出來的股數遠超過單檔 20% 上限
    h = {"A": bars("A", FLAT + [104, 105, 106]), "B": bars("B", FLAT + [110, 111, 112]),
         "C": bars("C", FLAT + [107, 108, 109])}
    r = run(h, max_positions=2)
    assert [o["symbol"] for o in r.orders if o["status"] == "filled"] == ["B", "C"]  # 漲幅大者優先
    assert max(p["positions"] for p in r.equity_curve) == 2


def test_cash_and_volume_limits():
    poor = run({"A": bars("A", FLAT + [104, 108, 112])}, initial_cash=50000, risk_pct=50,
               max_position_pct=500)
    assert fills(poor, "buy")[0][2] == 480 and poor.cash >= 0  # 50000 ÷ 104
    thin = run({"A": bars("A", FLAT + [104, 108, 112], volume=100)},
               execution=SmileExecution(slippage_bps=0, participation=.001, limit_guard=False,
                                        tick_rounding=False))
    assert fills(thin, "buy")[0][2] == 100  # 前一日 100 張 × 0.1% = 100 股


def test_ex_rights_credits_cash_and_lowers_stop_without_triggering_exit():
    closes = FLAT + [104, 108, 112, 106, 107, 108]
    h = {"A": bars("A", closes)}
    backtester = SwingBacktester(Settings(**SETTINGS), SwingParams(**{**SMALL, "trail_atr": 0.5}),
                                 execution=SmileExecution.ideal(), initial_cash=1000000)
    plain = backtester.run_histories(h)
    assert plain.rounds and plain.rounds[0].exit_reason == "trailing_stop"  # 沒調整：跳空被當成跌破
    event = dict(ex_date="2026-01-09", prev_close=112, reference_price=106, kind="息")
    adjusted = backtester.run_histories(h, ex_rights={"A": [event]})
    shares = fills(adjusted, "buy")[0][2]
    assert adjusted.distribution_total_twd == pytest.approx(6 * shares)
    assert adjusted.rounds == [] and len(adjusted.open_positions) == 1
    pos = adjusted.open_positions[0]
    assert pos["entry_price"] == pytest.approx(98) and pos["distribution_twd"] == 6 * shares
    assert adjusted.equity_curve[-1]["equity"] == pytest.approx(
        1000000 + (108 - 104) * shares + 6 * shares - 108 * shares * 0.003)


def test_data_end_forces_exit_at_last_close():
    h = {"A": bars("A", FLAT + [104, 108, 112]), "B": bars("B", [50] * 40)}
    r = run(h)
    record, = r.data_ended
    assert (record["symbol"], record["last_date"], record["forced_exit_date"],
            record["exit_price"]) == ("A", "2026-01-08", "2026-01-09", 112)
    assert r.rounds[0].exit_reason == "data_end" and r.open_positions == []


def test_random_entry_is_reproducible_and_keeps_exit_rules():
    h = {s: bars(s, [100 + (i * (3 + k)) % 17 for i in range(120)]) for k, s in enumerate("ABC")}
    backtester = SwingBacktester(Settings(**SETTINGS), SwingParams(**SMALL),
                                 execution=SmileExecution.ideal(), initial_cash=1000000)
    first = backtester.run_histories(h, random_entry=RandomEntry({"entry": 0.2}, "seed"))
    second = backtester.run_histories(h, random_entry=RandomEntry({"entry": 0.2}, "seed"))
    assert first.to_dict() == second.to_dict() and first.rounds
    assert {r.exit_reason for r in first.rounds} <= {"stop_loss", "trailing_stop", "expiry"}
    assert backtester.run_histories(h, random_entry=RandomEntry({}, "seed")).orders == []
    baseline = swing_random_baseline(backtester, h, runs=5, seed=3)
    assert baseline == swing_random_baseline(backtester, h, runs=5, seed=3)
    assert set(baseline["random_entry"]["rates"]) == {"entry"}
    assert baseline["random_entry"]["distribution"]["round_count"]["n"] == 5


def test_params_validation():
    with pytest.raises(ValueError):
        SwingParams(fast_ma=60, slow_ma=20)
    with pytest.raises(ValueError):
        SwingParams(risk_pct=0)
    with pytest.raises(ValueError):
        SwingBacktester(Settings(**SETTINGS), initial_cash=0)


def test_stop_exit_sells_within_volume_cap_across_days():
    # 買進後成交量萎縮到每天 500 張 → 0.1% 參與率每天只能賣 500 股；停損分多天賣完，算成同一輪。
    h = bars("A", FLAT + [104, 104, 95, 95, 95, 95, 95])
    for bar in h[6:]:
        bar.volume = 500
    model = SmileExecution(slippage_bps=0, participation=.001, limit_guard=False, tick_rounding=False)
    r = run({"A": h}, execution=model)
    (_, _, bought), = fills(r, "buy")
    sells = fills(r, "sell")
    assert [q for _, _, q in sells] == [500] * (bought // 500) + [bought % 500]
    assert sells[0][0] == "2026-01-09"
    rnd, = r.rounds
    assert rnd.exit_reason == "stop_loss" and rnd.exit_date == sells[-1][0]
    assert rnd.pnl_twd == pytest.approx((95 - 104) * bought - 95 * bought * 0.003)
    assert r.open_positions == []


# 規則二（entry="rs_revenue"）：相對強度排名 + 已公告的月營收年增率。
RS = dict(entry="rs_revenue", rs_min=0.6, yoy_min=0.0)
RISING = [100 + 2 * i for i in range(12)]
FLAT12 = [100] * 12
FALLING = [100 - 2 * i for i in range(12)]


def rs_histories():
    return {"A": bars("A", RISING), "B": bars("B", FLAT12), "C": bars("C", FALLING)}


def test_rs_revenue_buys_only_strong_symbols_with_growing_revenue():
    revenue = {"A": [("2025-12-11", 15.0)], "B": [("2025-12-11", 30.0)], "C": [("2025-12-11", 50.0)]}
    r = run(rs_histories(), revenue=revenue, **RS)
    assert {o["symbol"] for o in r.orders if o["side"] == "buy"} == {"A"}  # B、C 排名不夠
    first = next(o for o in r.orders if o["side"] == "buy")
    assert first["rs"] == 1.0


def test_rs_revenue_needs_published_positive_yoy():
    shrinking = {"A": [("2025-12-11", -5.0)]}
    assert run(rs_histories(), revenue=shrinking, **RS).orders == []
    # 營收要到 2026-01-09 才公告：之前的訊號不算。
    late = {"A": [("2026-01-09", 15.0)]}
    buys = [o for o in run(rs_histories(), revenue=late, **RS).orders if o["side"] == "buy"]
    assert buys and min(o["signal_date"] for o in buys) >= "2026-01-09"


def test_rs_revenue_requires_revenue_data_and_valid_params():
    with pytest.raises(ValueError):
        run(rs_histories(), **RS)
    with pytest.raises(ValueError):
        SwingParams(entry="other")
    with pytest.raises(ValueError):
        SwingParams(entry="rs_revenue", rs_min=1.0)


def test_load_revenue_shifts_availability_to_next_month():
    import sqlite3
    from types import SimpleNamespace
    from bot.swing_backtest import load_revenue
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE monthly_revenue (symbol TEXT, year_month TEXT, yoy_pct REAL)")
    conn.executemany("INSERT INTO monthly_revenue VALUES (?,?,?)",
                     [("A", "2025-11", 12.0), ("A", "2025-12", -3.0), ("A", "2026-01", None),
                      ("B", "2025-12", float("nan"))])
    out = load_revenue(SimpleNamespace(conn=conn), ["A", "B"])
    assert out == {"A": [("2025-12-11", 12.0), ("2026-01-11", -3.0)], "B": []}

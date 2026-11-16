"""隨機進場基準（#30）：可重現、只改進場、輸出分布。全部離線。"""
import json
import math

import pytest

from bot.config import Settings
from bot.random_entry import (RandomEntry, calibrate_rates, percentile, run_baseline,
                              smile_entry_rates, smile_random_baseline, summarize_distribution)
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.smile_execution import SmileExecution
from bot.stock_db import PriceBar


def wave(symbol, days=240, phase=0.0):
    """約 40 天一個週期、振幅 10% 的價格，足以讓階梯反覆買賣。"""
    out = []
    for i in range(days):
        close = round(100 + 10 * math.sin(i / 6.5 + phase), 2)
        month, day = divmod(i, 28)
        out.append(PriceBar(symbol, f"20{24 + month // 12}-{month % 12 + 1:02d}-{day + 1:02d}",
                            open=close, high=close + 1, low=close - 1, close=close, volume=100000))
    return out


def backtester(**overrides):
    values = dict(_env_file=None, max_fund=1000000, daily_fund_budget=0,
                  smile_buy_tiers="2:1,5:1", smile_base_lot=1, max_lot_per_symbol=5,
                  broker_fee_discount=0, broker_min_fee=0, smile_regular_tax_rate=0,
                  smile_symbol_stop_loss_pct=0, smile_max_unrealized_loss_pct=0)
    values.update(overrides)
    return SmileCurveBacktester(Settings(**values), execution=SmileExecution.ideal())


HISTORIES = {"A": wave("A"), "B": wave("B", phase=1.7)}


def test_same_seed_gives_identical_output():
    first = smile_random_baseline(backtester(), HISTORIES, runs=8, seed=7)
    second = smile_random_baseline(backtester(), HISTORIES, runs=8, seed=7)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    other = smile_random_baseline(backtester(), HISTORIES, runs=8, seed=8)
    assert other["random_entry"]["distribution"] != first["random_entry"]["distribution"]
    assert other["strategy"] == first["strategy"]  # 策略本身不受種子影響


def test_output_has_round_metric_distributions():
    result = smile_random_baseline(backtester(), HISTORIES, runs=8, seed=7)
    assert result["strategy"]["round_count"] > 0
    dist = result["random_entry"]["distribution"]
    for key in ("expectancy_pct", "expectancy_twd", "win_rate", "round_count", "hold_days_mean",
                "max_drawdown_pct", "annualized_return_pct", "filled_buys"):
        assert set(dist[key]) == {"n", "mean", "p5", "p50", "p95"}
        assert dist[key]["p5"] <= dist[key]["p50"] <= dist[key]["p95"]
    assert dist["round_count"]["n"] == 8
    assert dist["round_count"]["mean"] > 0
    assert result["random_entry"]["runs"] == 8 and result["random_entry"]["seed"] == 7
    assert set(result["random_entry"]["rates"]) == {"tier0", "tier1", "bottom"}
    assert isinstance(result["expectancy_pct_above_p95"], bool)
    calibration = result["random_entry"]["calibration"]
    assert calibration["target"] == result["strategy"]["filled_buys"]
    assert calibration["steps"] >= 1 and calibration["pilot_mean"] > 0


def test_calibration_scales_rates_towards_target():
    # 進場次數 = 機率 × 100：目標 30 時應把 0.1 放大到約 0.3。
    rates, info = calibrate_rates(lambda entry: entry.rates["x"] * 100, {"x": 0.1}, 30)
    assert rates["x"] == pytest.approx(0.3) and info["within_tolerance"]
    assert info["scale"] == pytest.approx(3.0) and info["steps"] == 2
    capped, info = calibrate_rates(lambda entry: entry.rates["x"] * 100, {"x": 0.5}, 500)
    assert capped["x"] == 1.0 and not info["within_tolerance"]  # 機率上限 1，做不到就照實標示
    assert calibrate_rates(lambda entry: 0, {}, 10)[0] == {}


def test_random_entry_only_changes_entries():
    bt = backtester()
    strategy = bt.run_histories(HISTORIES)
    rates = smile_entry_rates(strategy)
    assert all(0 < p < 1 for p in rates.values())
    random_run = bt.run_histories(HISTORIES, random_entry=RandomEntry(rates, "7:0"))
    buys = [o for o in random_run.orders if o["side"] == "buy"]
    assert buys and all(o["label"].startswith("random_") for o in buys)
    assert all(o["signal_date"] < o["execution_date"] for o in random_run.orders)
    # 出場規則不變：每一批都是回到標準價且整個部位有淨利才賣。
    assert random_run.all_round_trips
    assert {t.exit_reason for t in random_run.all_round_trips} == {"close_signal_next_open"}
    assert all(r.pnl_twd > 0 for r in random_run.rounds)
    # 部位大小不變：每階仍是 base_lot × 倍數。
    assert {o["quantity"] for o in buys} == {1}
    # 進場日不再跟著跌幅：有買單的訊號日收盤不低於標準價 2%（策略本身做不到）。
    closes = {(s, b.date): b.close for s, bars in HISTORIES.items() for b in bars}
    assert any(closes[(o["symbol"], o["signal_date"])] >= 108 for o in buys)
    assert all(closes[(o["symbol"], o["signal_date"])] < 108
               for o in strategy.orders if o["side"] == "buy")


def test_zero_rates_never_enter():
    quiet = backtester().run_histories(HISTORIES, random_entry=RandomEntry({}, "x"))
    assert quiet.orders == [] and quiet.net_pnl_twd == 0


def test_percentile_and_distribution_helpers():
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([10, 20], 5) == pytest.approx(10.5)
    assert percentile([4], 95) == 4
    with pytest.raises(ValueError):
        percentile([], 50)
    dist = summarize_distribution([dict(a=1, b=None, c=True), dict(a=3, b=2.0, c=False)])
    assert dist["a"] == dict(n=2, mean=2.0, p5=pytest.approx(1.1), p50=2.0, p95=pytest.approx(2.9))
    assert dist["b"]["n"] == 1 and "c" not in dist
    with pytest.raises(ValueError):
        run_baseline(lambda entry: {}, {}, runs=0)

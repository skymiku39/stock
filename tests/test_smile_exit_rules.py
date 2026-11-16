"""#31：停利、持有期限、再進場冷卻、階梯設定。日 K 引擎與組合回測；盤中在 test_simulation_check。"""
import pytest

from bot.config import Settings
from bot.round_metrics import summarize_rounds
from bot.smile_curve import SmileCurveEngine
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.smile_execution import SmileExecution
from bot.stock_db import PriceBar

BASE = dict(_env_file=None, max_fund=500000, daily_fund_budget=0, smile_buy_tiers="10:1,50:1",
            smile_base_lot=1, max_lot_per_symbol=1, broker_fee_discount=0, broker_min_fee=0,
            smile_regular_tax_rate=0, smile_symbol_stop_loss_pct=0,
            smile_max_unrealized_loss_pct=0)


def bars(symbol, closes):
    """開盤價 = 前一日收盤價，所以收盤訊號會在次日以該收盤價成交。"""
    opens = [closes[0]] + list(closes[:-1])
    return [PriceBar(symbol, f"2026-01-{i + 1:02d}", open=o, high=max(o, c) + 1,
                     low=min(o, c) - 1, close=c, volume=100000)
            for i, (o, c) in enumerate(zip(opens, closes))]


def run(closes, **overrides):
    settings = Settings(**{**BASE, **overrides})
    return SmileCurveBacktester(settings, execution=SmileExecution.ideal()).run_histories(
        {"A": bars("A", closes)})


def fills(result, side):
    return [(o["execution_date"], o["execution_price"]) for o in result.orders
            if o["side"] == side and o["status"] == "filled"]


def test_take_profit_sells_below_reference_once_net_profit_reaches_threshold():
    closes = [100, 90, 90, 95, 95, 95]
    assert run(closes).all_round_trips == []  # 未設停利：95 還沒回到標準價 100，不賣
    r = run(closes, smile_take_profit_pct=5)
    trip, = r.all_round_trips
    assert (trip.entry_date, trip.entry_price, trip.exit_date, trip.exit_price) == (
        "2026-01-03", 90, "2026-01-05", 95)
    assert summarize_rounds(r.rounds)["exit_reason_counts"] == {"take_profit": 1}
    # 淨利 4.4% 未達 5% 不賣。
    assert run([100, 90, 90, 94, 94, 94], smile_take_profit_pct=5).all_round_trips == []


def test_expiry_sells_at_a_loss_resets_reference_and_starts_cooldown():
    closes = [100, 90, 88, 88, 88, 88, 70, 70, 70, 70, 70, 70, 70, 70]
    r = run(closes, smile_max_hold_days=3, smile_reentry_cooldown_days=5)
    # 01-03 成交，01-06 滿 3 天發訊號，01-07 開盤賣出（認賠），冷卻到 01-12。
    assert fills(r, "sell") == [("2026-01-07", 88)]
    assert fills(r, "buy") == [("2026-01-03", 90), ("2026-01-13", 70)]
    assert summarize_rounds(r.rounds)["exit_reason_counts"] == {"expiry": 1}
    assert r.rounds[0].pnl_twd == -2000 and r.rounds[0].hold_days == 4
    assert r.results[0].stopped_date is None  # 到期出場不停用
    # 沒有冷卻時，出場當天收盤就能再發買進訊號。
    quick = run(closes, smile_max_hold_days=3)
    assert fills(quick, "buy") == [("2026-01-03", 90), ("2026-01-08", 70)]
    # 沒有期限時一直抱著。
    assert fills(run(closes), "sell") == []


def test_profitable_expiry_does_not_start_cooldown():
    closes = [100, 90, 95, 95, 95, 95, 80, 80, 80]
    r = run(closes, smile_max_hold_days=3, smile_reentry_cooldown_days=5)
    assert fills(r, "sell") == [("2026-01-07", 95)]
    assert r.rounds[0].pnl_twd == 5000 and r.rounds[0].exit_reason == "expiry"
    assert fills(r, "buy")[1] == ("2026-01-08", 80)  # 標準價重設為 95，跌 15.8% 立刻再買


def test_take_profit_is_checked_before_expiry():
    r = run([100, 90, 90, 90, 90, 96, 96], smile_max_hold_days=3, smile_take_profit_pct=5)
    assert summarize_rounds(r.rounds)["exit_reason_counts"] == {"take_profit": 1}


def test_zero_settings_match_defaults():
    closes = [100, 97, 95, 90, 101, 103, 99, 93, 96, 104, 100, 92, 105]
    explicit = run(closes, smile_take_profit_pct=0, smile_max_hold_days=0,
                   smile_reentry_cooldown_days=0, smile_bottom_dca_enabled=True,
                   smile_min_first_tier_pct=0, smile_buy_tiers="1:1,3:1", max_lot_per_symbol=5)
    default = run(closes, smile_buy_tiers="1:1,3:1", max_lot_per_symbol=5)
    a, b = explicit.to_dict(), default.to_dict()
    assert a.pop("settings_note") == b.pop("settings_note")
    assert a == b and default.all_round_trips


def test_bottom_dca_can_be_switched_off():
    closes = [100, 97, 96, 95, 94]
    labels = lambda r: [o["label"] for o in r.orders if o["side"] == "buy"]
    on = run(closes, smile_buy_tiers="1:1,2:1", max_lot_per_symbol=5)
    off = run(closes, smile_buy_tiers="1:1,2:1", max_lot_per_symbol=5,
              smile_bottom_dca_enabled=False)
    assert "bottom_dca" in labels(on)
    assert labels(off) == ["dip_1.0%", "dip_2.0%"]


def test_first_tier_floor_rejects_shallow_ladders():
    with pytest.raises(ValueError, match="SMILE_MIN_FIRST_TIER_PCT"):
        SmileCurveEngine("A", Settings(**{**BASE, "smile_buy_tiers": "1:1,3:2",
                                          "smile_min_first_tier_pct": 3}))
    SmileCurveEngine("A", Settings(**{**BASE, "smile_buy_tiers": "3:1,6:2,10:3",
                                      "smile_min_first_tier_pct": 3}))
    with pytest.raises(ValueError):
        Settings(**{**BASE, "smile_max_hold_days": -1})


def test_daily_engine_expiry_and_cooldown():
    e = SmileCurveEngine("A", Settings(**{**BASE, "smile_buy_tiers": "10:1",
                                          "smile_bottom_dca_enabled": False,
                                          "smile_max_hold_days": 3,
                                          "smile_reentry_cooldown_days": 5}))
    e.on_bar("2026-01-01", 100)
    assert [a.kind for a in e.on_bar("2026-01-02", 90)] == ["buy"]
    assert e.on_bar("2026-01-04", 88) == []
    actions = e.on_bar("2026-01-05", 88)
    assert [(a.kind, a.reason) for a in actions][0] == ("sell", "expiry")
    assert actions[-1].kind == "reset" and not e.open_lots and not e.state.disabled
    assert e.state.reference == 88 and e.state.cooldown_until == "2026-01-10"
    assert e.round_trips[0].exit_reason == "expiry" and e.round_trips[0].pnl_twd == -2000
    assert e.on_bar("2026-01-09", 70) == []  # 冷卻中
    assert [a.kind for a in e.on_bar("2026-01-10", 70)] == ["buy"]


def test_daily_engine_take_profit():
    e = SmileCurveEngine("A", Settings(**{**BASE, "smile_buy_tiers": "10:1",
                                          "smile_bottom_dca_enabled": False,
                                          "smile_take_profit_pct": 5}))
    e.on_bar("2026-01-01", 100)
    e.on_bar("2026-01-02", 90)
    assert e.on_bar("2026-01-03", 94) == []
    assert [a.kind for a in e.on_bar("2026-01-04", 95)] == ["sell", "reset"]

"""#32：依權益調整部位（複利）。權益＝起始資金＋累計已實現損益，不計未實現。"""
import pytest

from bot.config import Settings
from bot.smile_curve import SmileAccount, SmileCurveEngine, smile_equity, smile_fund_cap
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.smile_execution import SmileExecution
from bot.stock_db import PriceBar

BASE = dict(_env_file=None, max_fund=1000000, daily_fund_budget=0, smile_buy_tiers="10:1,50:1",
            smile_base_lot=1, max_lot_per_symbol=100, use_odd_lot=True, broker_fee_discount=0,
            broker_min_fee=0, smile_regular_tax_rate=0, smile_symbol_stop_loss_pct=0,
            smile_max_unrealized_loss_pct=0)
COMPOUND = dict(smile_initial_fund=100000, smile_unit_equity_pct=10)


def bars(symbol, closes):
    opens = [closes[0]] + list(closes[:-1])
    return [PriceBar(symbol, f"2026-01-{i + 1:02d}", open=o, high=max(o, c) + 1,
                     low=min(o, c) - 1, close=c, volume=100000)
            for i, (o, c) in enumerate(zip(opens, closes))]


def run(closes, **overrides):
    settings = Settings(**{**BASE, **overrides})
    return SmileCurveBacktester(settings, execution=SmileExecution.ideal()).run_histories(
        {"A": bars("A", closes)})


def buys(result):
    return [(o["execution_date"], o["filled_quantity"]) for o in result.orders
            if o["side"] == "buy" and o["status"] == "filled"]


def test_next_round_buys_more_after_a_gain():
    r = run([100, 90, 90, 100, 100, 90, 90], **COMPOUND)
    # 第一輪：100000 × 10% ÷ 90 = 111 股，賺 1110；第二輪權益 101110 → 112 股。
    assert buys(r) == [("2026-01-03", 111), ("2026-01-07", 112)]
    assert r.initial_cash == 100000 and r.total_pnl_twd == 1110
    assert r.equity_curve[0]["equity"] == 100000


def test_next_round_buys_less_after_a_loss():
    closes = [100, 90, 90, 80, 80, 80, 80, 80, 72, 72]
    r = run(closes, smile_max_hold_days=3, **COMPOUND)
    # 第一輪 111 股 @90，到期以 80 認賠 1110；第二輪權益 98890，在 72 買 137 股（9864 元）。
    assert buys(r) == [("2026-01-03", 111), ("2026-01-10", 137)]
    assert r.total_pnl_twd == -1110
    flat = run(closes, smile_max_hold_days=3, smile_initial_fund=100000)
    assert buys(flat) == [("2026-01-03", 1), ("2026-01-10", 1)]  # 沒設單位比例：沿用 base_lot


def test_exposure_never_exceeds_max_fund_or_equity():
    capped = run([100, 90, 90, 100, 100, 90, 90], max_fund=5000, **COMPOUND)
    assert buys(capped)[0] == ("2026-01-03", 55)  # 5000 ÷ 90
    assert all(p["cost_exposure"] <= 5000 for p in capped.equity_curve)
    # 單位比例很大時，受權益（起始資金）限制，不會用到 MAX_FUND 的一百萬。
    greedy = run([100, 90, 90, 90], smile_initial_fund=100000, smile_unit_equity_pct=500)
    assert buys(greedy) == [("2026-01-03", 1111)]
    assert all(p["cost_exposure"] <= 100000 and p["cash"] >= 0 for p in greedy.equity_curve)


def test_whole_lot_mode_skips_when_amount_is_below_one_lot():
    r = run([100, 90, 90, 90], use_odd_lot=False, **COMPOUND)  # 10000 元買不起一張 90000
    assert buys(r) == [] and r.orders == [] and r.results[0].pending_orders == 0
    big = run([100, 90, 90, 90], use_odd_lot=False, smile_initial_fund=1000000,
              smile_unit_equity_pct=20)
    assert buys(big) == [("2026-01-03", 2)]  # 200000 ÷ 90000 → 2 張


def test_zero_settings_match_defaults():
    closes = [100, 97, 95, 90, 101, 103, 99, 93, 96, 104, 100, 92, 105]
    kwargs = dict(smile_buy_tiers="1:1,3:1", use_odd_lot=False, max_lot_per_symbol=5)
    explicit = run(closes, smile_initial_fund=0, smile_unit_equity_pct=0, **kwargs)
    default = run(closes, **kwargs)
    assert explicit.to_dict() == default.to_dict() and default.all_round_trips
    # 只設單位比例、沒設起始資金：視為未啟用。
    unit_only, expected = run(closes, smile_unit_equity_pct=10, **kwargs).to_dict(), default.to_dict()
    assert unit_only.pop("settings_note") != expected.pop("settings_note")
    assert unit_only == expected


def test_equity_ignores_unrealized_and_accounts_are_shared_across_symbols():
    settings = Settings(**{**BASE, **COMPOUND})
    account = SmileAccount()
    assert smile_equity(settings, account) == 100000
    assert smile_fund_cap(settings, account) == 100000
    account.realized_pnl_twd = 2000000
    assert smile_fund_cap(settings, account) == 1000000  # MAX_FUND 是硬上限
    account.realized_pnl_twd = -150000
    assert smile_equity(settings, account) == 0 and smile_fund_cap(settings, account) == 0
    assert smile_equity(Settings(**BASE), account) is None
    assert smile_fund_cap(Settings(**BASE), account) == 1000000
    # 兩檔共用現金：A 賺的錢會放大 B 之後的部位。
    h = {"A": bars("A", [100, 90, 90, 100, 100, 100, 100]),
         "B": bars("B", [50, 50, 50, 50, 50, 45, 45])}
    r = SmileCurveBacktester(settings, execution=SmileExecution.ideal()).run_histories(h)
    by_symbol = {o["symbol"]: o["filled_quantity"] for o in r.orders
                 if o["side"] == "buy" and o["status"] == "filled"}
    assert by_symbol == {"A": 111, "B": 224}  # B：101110 × 10% ÷ 45


def test_daily_engine_sizes_from_its_own_realized_pnl():
    e = SmileCurveEngine("A", Settings(**{**BASE, **COMPOUND, "smile_buy_tiers": "10:1",
                                          "smile_bottom_dca_enabled": False}))
    e.on_bar("2026-01-01", 100)
    assert e.on_bar("2026-01-02", 90)[0].quantity == 111
    e.on_bar("2026-01-03", 100)
    assert e.account.realized_pnl_twd == 1110
    assert e.on_bar("2026-01-04", 90)[0].quantity == 112
    with pytest.raises(ValueError):
        Settings(**{**BASE, "smile_unit_equity_pct": -1})

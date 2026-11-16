"""日 K 回測的除權息調整（#29），完全離線。"""
from __future__ import annotations

from pathlib import Path

import pytest

from bot.config import Settings
from bot.ex_rights import save_events
from bot.smile_curve import SmileCurveEngine, SmileLot
from bot.smile_curve_backtest import SmileCurveBacktester, ex_rights_adjustment
from bot.smile_curve_backtest_analysis import BacktestScenario, run_scenario
from bot.smile_execution import SmileExecution
from bot.stock_db import PriceBar, StockDB


def bars(symbol, prices):
    return [PriceBar(symbol, f"2026-01-{i+1:02d}", open=o, high=max(o, c), low=min(o, c),
                     close=c, volume=100000) for i, (o, c) in enumerate(prices)]


def settings(**overrides):
    values = dict(_env_file=None, max_fund=500000, daily_fund_budget=0,
                  smile_buy_tiers="1:1,50:1", smile_base_lot=1, max_lot_per_symbol=1,
                  broker_fee_discount=0, broker_min_fee=0, smile_regular_tax_rate=0,
                  smile_symbol_stop_loss_pct=0, smile_max_unrealized_loss_pct=0)
    values.update(overrides)
    return Settings(**values)


def run(histories, ex_rights=None, **overrides):
    return SmileCurveBacktester(settings(**overrides), execution=SmileExecution.ideal()).run_histories(
        histories, ex_rights=ex_rights)


def event(symbol, day, prev_close, reference_price, kind="息"):
    return dict(symbol=symbol, ex_date=f"2026-01-{day:02d}", kind=kind, prev_close=prev_close,
                reference_price=reference_price,
                value_total=None if reference_price is None else prev_close - reference_price,
                cash_dividend=None, stock_per_1000=None, market="TWSE", source_url="",
                retrieved_at="")


# 第 1 天定標準價 100；第 2 天收 98 出買進訊號；第 3 天開盤 95 買 1 張。
HELD = [(100, 100), (100, 98), (95, 96)]


def test_distribution_credits_cash_and_adjusts_basis_and_reference():
    history = {"A": bars("A", HELD + [(91, 92), (96, 96), (97, 97)])}
    r = run(history, {"A": [event("A", 4, 96, 91)]})
    d, = r.distributions
    assert (d["status"], d["applied_date"], d["shares"], d["amount_twd"]) == (
        "applied", "2026-01-04", 1000, 5000)
    assert d["reference_before"] == 100
    assert d["reference_after"] == pytest.approx(100 * 91 / 96)
    assert r.distribution_total_twd == 5000
    # 標準價下修後，第 5 天收 96 已回到標準價之上，第 6 天開盤 97 全賣。
    t, = r.all_round_trips
    assert (t.entry_price, t.exit_price, t.distribution_twd) == (95, 97, 5000)
    assert t.pnl_twd == 7000  # 價差 2,000 + 配發 5,000
    assert r.cash == pytest.approx(r.initial_cash + r.net_pnl_twd)
    rd, = r.rounds
    assert rd.cost_twd == 95000  # 回合報酬率以原始投入為分母
    assert r.to_dict()["distribution_total_twd"] == 5000

    plain = run(history)
    assert plain.all_round_trips == [] and plain.distributions == []


def test_ex_dividend_gap_does_not_trigger_stop_loss():
    history = {"A": bars("A", HELD + [(80, 80), (80, 80), (80, 80)])}
    adjusted = run(history, {"A": [event("A", 4, 96, 80)]}, smile_symbol_stop_loss_pct=10)
    assert adjusted.results[0].stopped_date is None
    assert adjusted.all_round_trips == []
    assert adjusted.results[0].unrealized_pnl_twd == pytest.approx(1000)  # 成本 79、市價 80

    plain = run(history, smile_symbol_stop_loss_pct=10)
    assert plain.results[0].stopped_date == "2026-01-05"


def test_no_events_leaves_results_unchanged():
    history = {"A": bars("A", [(100, 100), (100, 98), (95, 102), (104, 104), (104, 99), (97, 106)]),
               "B": bars("B", [(50, 50), (50, 49), (48, 51), (52, 52)])}
    plain, empty = run(history).to_dict(), run(history, {}).to_dict()
    # 另一檔有事件，不影響沒有事件的 A。
    other = run(history, {"B": [event("B", 9, 52, 50)]}).to_dict()
    for key in ("orders", "equity_curve", "cash", "rounds", "net_pnl_twd"):
        assert plain[key] == empty[key] == other[key]
    assert empty["distributions"] == []
    assert (plain["ex_rights_no_events"], empty["ex_rights_no_events"],
            other["ex_rights_no_events"]) == ([], ["A", "B"], ["A"])


def test_unusable_event_pauses_signals_for_that_day():
    history = {"A": bars("A", [(100, 100), (100, 98), (98, 98)])}
    r = run(history, {"A": [event("A", 2, 100, None)]})
    d, = r.distributions
    assert (d["status"], d["reason"]) == ("skipped", "missing_or_invalid_reference")
    assert r.orders == []  # 第 2 天的買進訊號被暫停
    assert r.results[0].pending_orders == 1  # 第 3 天恢復
    assert run(history).orders != []


def test_flat_symbol_only_scales_reference():
    history = {"A": bars("A", [(100, 100), (90, 90), (90, 90)])}
    r = run(history, {"A": [event("A", 2, 100, 90, kind="權")]})
    d, = r.distributions
    assert (d["shares"], d["amount_twd"], d["reference_after"]) == (0, 0, pytest.approx(90))
    assert r.orders == [] and r.results[0].pending_orders == 0 and r.cash == r.initial_cash
    assert run(history).orders != []  # 未調整時，除權跳空被當成 10% 跌幅而買進


def test_event_before_tracking_and_late_bar():
    # 事件早於第一根 K：不調整也不記錄。除權息日沒有 K：在之後第一根 K 套用。
    history = {"A": [b for b in bars("A", HELD + [(91, 92), (91, 92)]) if b.date != "2026-01-04"]}
    r = run(history, {"A": [event("A", 1, 110, 100), event("A", 4, 96, 91)]})
    d, = r.distributions
    assert (d["ex_date"], d["applied_date"], d["amount_twd"]) == ("2026-01-04", "2026-01-05", 5000)


def test_manual_reference_is_not_scaled():
    engine = SmileCurveEngine("A", settings(smile_reference_prices={"A": 100.0}))
    engine.state.reference = 100.0
    engine.state.lots = [SmileLot(95.0, 1, "lot", "2026-01-03")]
    assert engine.apply_distribution(5.0, 0.95) == 5000
    assert engine.state.reference == 100.0
    assert (engine.state.lots[0].entry_price, engine.state.lots[0].distribution_per_share) == (90.0, 5.0)


@pytest.mark.parametrize("prev,ref", [(None, 90), (100, None), (100, 0), (100, 100), (90, 100),
                                      ("x", 90)])
def test_adjustment_rejects_unusable_rows(prev, ref):
    assert ex_rights_adjustment(dict(prev_close=prev, reference_price=ref)) is None


def test_run_many_loads_events_from_db_and_can_disable(tmp_path: Path):
    db = StockDB.open(path=tmp_path / "smile.db")
    try:
        db.bulk_upsert_price_bars(bars("2317", HELD + [(91, 92), (96, 96), (97, 97)]))
        save_events(db, [event("2317", 4, 96, 91)])
        ideal = SmileExecution.ideal()
        on = SmileCurveBacktester(settings(), execution=ideal).run_many(["2317"], db=db)
        off = SmileCurveBacktester(settings(), execution=ideal,
                                   apply_ex_rights=False).run_many(["2317"], db=db)
        assert on.distribution_total_twd == 5000 and len(on.all_round_trips) == 1
        assert off.distributions == [] and off.all_round_trips == []
        assert "未調整" in off.settings_note and "未調整" not in on.settings_note

        # 單檔分析路徑（收盤價引擎）套用同一套調整。
        report = run_scenario(db, "2317", BacktestScenario(
            label="close", start=None, end=None, max_fund=500000, use_odd_lot=False,
            smile_buy_tiers="1:1,50:1", smile_base_lot=1, price_mode="close"),
            settings=settings())
        trip, = report.round_trips
        assert trip.distribution_twd == 5000
    finally:
        db.close()

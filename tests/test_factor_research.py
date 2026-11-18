"""因子研究：統計函式、季報換算、前視檢查，以及用合成資料確認管線找得到埋進去的因子。"""

import random
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from bot import factor_research as fr


def _calendar(start: date, days: int) -> list:
    out, d = [], start
    while len(out) < days:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _bars(dates, closes, volume=5000.0):
    return [SimpleNamespace(date=d, open=c, high=c * 1.01, low=c * 0.99, close=c, volume=volume)
            for d, c in zip(dates, closes)]


def _panel(n_symbols=30, days=900, seed=7, drift_scale=0.002):
    """每檔有固定的每日漂移；quality 因子直接回傳漂移，所以應該有很高的 IC。"""
    rng = random.Random(seed)
    dates = _calendar(date(2020, 1, 2), days)
    series, drift = {}, {}
    for k in range(n_symbols):
        sym = f"S{k:02d}"
        drift[sym] = (k - n_symbols / 2) / n_symbols * drift_scale
        price, closes = 100.0, []
        for _ in dates:
            price *= 1 + drift[sym] + rng.gauss(0, 0.01)
            closes.append(price)
        series[sym] = fr.build_series(sym, _bars(dates, closes))
    return fr.Panel(series, {}, {}), drift


def test_ranks_average_ties():
    assert fr.ranks([10, 20, 20, 5]) == [2.0, 3.5, 3.5, 1.0]


def test_spearman_bounds_and_degenerate():
    assert fr.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert fr.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert fr.spearman([1, 1, 1], [1, 2, 3]) is None


def test_t_stat_discounts_overlapping_horizon():
    values = [0.1, 0.05, 0.12, 0.02, 0.08, 0.09]
    assert fr.t_stat(values, 42) == pytest.approx(fr.t_stat(values, 21) / 2 ** 0.5)


def test_single_quarter_rows_decumulates_and_sets_available_date():
    rows = [("2023Q1", 1.0, 10.0, 5.0), ("2023Q2", 3.0, 11.0, 6.0), ("2023Q4", 9.0, 12.0, 7.0)]
    out = {r[1]: r for r in fr.single_quarter_rows(rows)}
    assert out["2023Q1"][:3] == ("2023-05-16", "2023Q1", 1.0)
    assert out["2023Q2"][:3] == ("2023-09-01", "2023Q2", 2.0)
    assert out["2023Q4"][0] == "2024-04-01"
    assert out["2023Q4"][2] is None          # 缺 Q3，算不出單季


def test_eval_dates_take_first_trading_day_on_or_after_11th():
    cal = ["2024-01-10", "2024-01-12", "2024-01-15", "2024-02-09", "2024-02-15", "2024-03-11"]
    assert fr.eval_dates(cal) == ["2024-01-12", "2024-02-15", "2024-03-11"]


def test_fundamental_factors_respect_available_date():
    dates = _calendar(date(2023, 1, 2), 300)
    s = fr.build_series("X", _bars(dates, [50.0] * len(dates)))
    quarterly = {"X": fr.single_quarter_rows(
        [("2022Q1", 1.0, 10, 5), ("2022Q2", 2.0, 10, 5), ("2022Q3", 3.0, 10, 5), ("2022Q4", 4.0, 10, 5),
         ("2023Q1", 2.0, 12, 8)])}
    panel = fr.Panel({"X": s}, {"X": [("2023-03-11", 5.0), ("2023-04-11", 20.0)]}, quarterly)
    before, after = s.index["2023-03-31"], s.index["2023-04-03"]
    assert fr.f_earnings_yield(s, before, panel) is None            # 2022Q4 要 04-01 才知道
    assert fr.f_earnings_yield(s, after, panel) == pytest.approx(4.0 / 50.0)
    assert fr.f_rev_yoy(s, s.index["2023-04-10"], panel) == 5.0
    assert fr.f_rev_yoy(s, s.index["2023-04-11"], panel) == 20.0
    may = s.index["2023-05-16"]
    assert fr.f_eps_surprise(s, may, panel) == pytest.approx((2.0 - 1.0) / 50.0)
    assert fr._margin_change(4)(s, may, panel) == 3


def test_scores_do_not_change_when_future_prices_change():
    panel, _ = _panel(days=400)
    cut = fr.eval_dates(panel.calendar)[-3]
    price_factors = [f for f in fr.FACTORS if f.family in ("momentum", "reversal", "risk", "liquidity")]
    before = fr.score_dates(panel, price_factors)[cut]
    for s in panel.series.values():
        for k in range(s.index[cut] + 1, len(s.dates)):
            s.close[k] *= 3
            s.open[k] *= 3
            s.raw_close[k] *= 3
            s.volume[k] *= 3
    assert fr.score_dates(panel, price_factors)[cut] == before


def test_pipeline_finds_planted_factor_and_rejects_noise():
    panel, drift = _panel()
    rng = random.Random(1)
    factors = (
        fr.Factor("planted", "a", "漂移", lambda s, i, p: drift[s.symbol]),
        fr.Factor("inverse", "b", "漂移取負", lambda s, i, p: -drift[s.symbol]),
        fr.Factor("noise", "c", "雜訊", lambda s, i, p: rng.random()),
    )
    result = fr.evaluate(panel, factors, min_value=0)
    planted = result["factors"]["planted"]["horizons"]["20"]["train"]
    assert planted["ic_mean"] > 0.1 and planted["ic_t"] > 2
    assert planted["groups"][-1] > planted["groups"][0]
    assert result["factors"]["planted"]["pass"] is True
    inverse = result["factors"]["inverse"]["horizons"]["20"]["train"]
    assert inverse["ic_mean"] == pytest.approx(-planted["ic_mean"])
    assert result["factors"]["noise"]["pass"] is False
    assert result["correlation_train"]["planted|inverse"] == pytest.approx(-1.0)
    wf = result["walk_forward"]
    assert wf["first_date"]
    first = wf["selection"][wf["first_date"]]
    assert "+planted" in first and "-inverse" in first
    assert wf["horizons"]["20"]["all"]["ic_mean"] > 0.1
    assert "planted" in fr.render(result)


def test_walk_forward_only_uses_past_ic():
    dates = [f"2020-{m:02d}-11" for m in range(1, 13)] + [f"2021-{m:02d}-11" for m in range(1, 13)] \
        + [f"2022-{m:02d}-11" for m in range(1, 7)]
    factors = (fr.Factor("f", "fam", "", lambda s, i, p: None),)
    scored = {d: {"scores": {"f": {f"S{k}": float(k) for k in range(5)}}} for d in dates}
    rng = random.Random(3)
    ics = {"f": {d: 0.1 + rng.gauss(0, 0.02) for d in dates}}
    composite, chosen = fr.walk_forward_scores(dates, scored, ics, factors)
    lag, need = fr.WF["lag_periods"], fr.WF["min_periods"]
    assert min(chosen) == dates[need + lag - 1]      # 第一次選得出來時，剛好累積到 need 期較早的 IC
    assert composite[min(chosen)]["S4"] == 1.0


def test_eligibility_uses_recent_traded_value():
    dates = _calendar(date(2024, 1, 2), 30)
    thin = fr.build_series("T", _bars(dates, [10.0] * 30, volume=100.0))      # 10 元 × 100 張 = 100 萬
    assert not fr.eligible(thin, 29, fr.MIN_VALUE_TWD)
    assert fr.eligible(thin, 29, 1_000_000)
    assert not fr.eligible(thin, 10, 0)

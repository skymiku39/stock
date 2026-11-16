"""比較腳本（#19）：選定規則、判定、逐年損益、重抽樣，以及用合成資料跑完整流程。全部離線。"""
import datetime as dt
import json
import math

import pytest

from bot import strategy_comparison as sc
from bot.stock_db import PriceBar
from bot.strategy_comparison_report import render


def combos(values):
    """values：3×3 的參數決定段期望值。"""
    out = sc.grid(sc.SWING_AXES)
    for combo, value in zip(out, [v for row in values for v in row]):
        combo["train"] = dict(expectancy_pct=value, round_count=10)
    return out


def test_grid_has_nine_combos_and_neighbours_are_adjacent():
    grid = sc.grid(sc.SMILE_AXES)
    assert len(grid) == 9 and len(sc.grid(sc.SWING_AXES)) == 9
    assert sorted(c["index"] for c in sc.neighbours((1, 1), grid)) == [(0, 1), (1, 0), (1, 2), (2, 1)]
    assert sorted(c["index"] for c in sc.neighbours((0, 0), grid)) == [(0, 1), (1, 0)]


def test_choose_requires_positive_neighbours():
    # 最高的 9 旁邊有負值，不選；選自己與相鄰都為正的組合中最高者。
    chosen = sc.choose(combos([[9, -1, 2], [1, 3, 4], [2, 5, 1]]))
    assert chosen["index"] == (2, 1) and chosen["neighbours_all_positive"]
    fallback = sc.choose(combos([[5, -1, 2], [-1, 3, -1], [2, -1, 1]]))
    assert fallback["index"] == (0, 0) and not fallback["neighbours_all_positive"]
    tie = sc.choose(combos([[2, 2, 2], [2, 2, 2], [2, 2, 2]]))
    assert tie["index"] == (0, 0)  # 同分時取格子順序最前面的


def test_yearly_pnl_and_bootstrap():
    curve = [dict(date="2024-06-01", equity=110), dict(date="2024-12-31", equity=120),
             dict(date="2025-03-01", equity=90), dict(date="2026-10-30", equity=95)]
    assert sc.yearly_pnl(curve, 100) == {"2024": 20, "2025": -30, "2026": 5}
    assert sc.yearly_pnl([], 100) == {}
    assert sc.bootstrap_p5([]) is None
    assert sc.bootstrap_p5([2.0] * 30) == pytest.approx(2.0)
    spread = sc.bootstrap_p5([5.0, -4.0, 3.0, -2.0, 6.0, 1.0] * 5)
    assert spread == sc.bootstrap_p5([5.0, -4.0, 3.0, -2.0, 6.0, 1.0] * 5)  # 種子固定
    assert -1.0 < spread < 1.5


def metrics(**overrides):
    base = dict(round_count=80, expectancy_pct=2.0, max_drawdown_pct=12.0, hold_days_mean=40.0)
    return {**base, **overrides}


def test_judge_needs_every_core_condition():
    good = dict(metrics=metrics(), yearly_pnl_twd={"2024": 1, "2025": 2, "2026": 3})
    verdict = sc.judge(good, 0.5, 1.5)
    assert verdict["has_evidence"] and all(r["met"] for r in verdict["reference"])
    cases = [
        (dict(good, metrics=metrics(expectancy_pct=-0.1)), 0.5, 1.5),
        (good, -0.1, 1.5),
        (good, None, 1.5),
        (dict(good, yearly_pnl_twd={"2024": 1, "2025": -2, "2026": 3}), 0.5, 1.5),
        (good, 0.5, 2.5),
        (good, 0.5, None),
        (dict(good, metrics=metrics(max_drawdown_pct=20.1)), 0.5, 1.5),
    ]
    assert [sc.judge(*case)["has_evidence"] for case in cases] == [False] * 7
    # 參考值不影響成敗，只加註記。
    thin = sc.judge(dict(good, metrics=metrics(round_count=20, hold_days_mean=120)), 0.5, 1.5)
    assert thin["has_evidence"]
    assert [r["note"] for r in thin["reference"]] == ["樣本不足，屬初步證據", "不符合短中期定位"]


def synthetic(symbol, start, days, phase):
    """緩步上漲加上約 30 天週期的波動，兩種策略都會有交易。"""
    out, day = [], dt.date.fromisoformat(start)
    for i in range(days):
        close = round(50 + 0.03 * i + 6 * math.sin(i / 5 + phase), 2)
        previous = out[-1].close if out else close
        out.append(PriceBar(symbol, (day + dt.timedelta(days=i)).isoformat(), open=previous,
                            high=max(previous, close) + 0.5, low=min(previous, close) - 0.5,
                            close=close, volume=5000000))
    return out


def test_compare_runs_end_to_end_and_report_renders(monkeypatch):
    monkeypatch.setattr(sc, "SWING_AXES", (("breakout_days", (5, 8, 10)), ("trail_atr", (2.0, 3.0, 4.0))))
    monkeypatch.setattr(sc, "_swing_backtester", lambda p, make=sc._swing_backtester: make(
        dict(fast_ma=5, slow_ma=15, atr_days=5, momentum_days=10, **p)))
    symbols = ["1101", "1102", "1103"]
    def histories(start, days):
        return {s: synthetic(s, start, days, k * 1.3) for k, s in enumerate(symbols)}, None
    data = dict(train=histories("2020-01-02", 300), valid=histories("2024-01-01", 300),
                warm=histories("2023-11-01", 361))
    result = sc.compare(symbols, data, runs=3)
    assert result == sc.compare(symbols, data, runs=3)  # 可重現
    json.dumps(result)  # 結果可以直接存成 JSON
    for key in ("smile", "swing"):
        s = result["strategies"][key]
        assert len(s["combos"]) == 9 and sum(c["chosen"] for c in s["combos"]) == 1
        assert len(s["verdict"]["core"]) == 5 and isinstance(s["verdict"]["has_evidence"], bool)
        assert s["random_entry"]["runs"] == 3
        assert set(s["validation"]["yearly_pnl_twd"]) == {"2024"}
    assert result["strategies"]["swing"]["validation"]["metrics"]["round_count"] > 0
    report = render(result)
    for text in ("## 結論", "## 驗證段並列", "通過標準逐項判定", "隨機進場基準", "## 限制", "## 重現",
                 "**選定**", "最差的五檔"):
        assert text in report
    train_only = sc.compare(symbols, data, validate=False)
    assert "validation" not in train_only["strategies"]["smile"]
    assert "驗證段尚未執行" in render(train_only) and "## 結論" not in render(train_only)

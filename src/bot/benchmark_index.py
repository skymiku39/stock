"""benchmark_index -- 回測名單的等權指數（通過標準「回撤不超過大盤」用）。

指數 = 每個交易日，名單內當天與前一天都有收盤價的標的，日報酬取平均後累乘（每日再平衡）。
除權息日的報酬以「前一日收盤 × 參考價 ÷ 前一日收盤價」為基準，所以配發視為再投入（總報酬指數）。
單日報酬超過 ±30% 的視為資料問題，不計入。

為什麼用等權指數而不是加權指數：策略只在這 60 檔裡選股、每檔部位相近，等權指數才是同一個母體的
「什麼都不挑、全部持有」。這裡只拿它的回撤當風險比較，不拿報酬當比較基準。
"""

from __future__ import annotations

import math
from typing import Optional

from bot.smile_curve_backtest import ex_rights_adjustment


def equal_weight_levels(histories: dict, ex_rights: Optional[dict] = None) -> list[tuple[str, float]]:
    """[(日期, 指數水準)]，第一個交易日前的水準視為 1。"""
    factors: dict = {}
    for sym, events in (ex_rights or {}).items():
        for ev in events or []:
            adj = ex_rights_adjustment(ev)
            if adj and 0 < adj[1] <= 1:
                factors[(sym, str(ev["ex_date"])[:10])] = adj[1]
    daily: dict = {}
    for sym, bars in histories.items():
        prev = None
        for bar in sorted(bars, key=lambda b: b.date):
            close = bar.close
            if close is None or not math.isfinite(float(close)) or float(close) <= 0:
                continue
            date = bar.date[:10]
            if prev is not None:
                base = prev * factors.get((sym, date), 1.0)
                r = float(close) / base - 1.0
                if abs(r) < 0.3:
                    daily.setdefault(date, []).append(r)
            prev = float(close)
    level, out = 1.0, []
    for date in sorted(daily):
        level *= 1.0 + sum(daily[date]) / len(daily[date])
        out.append((date, level))
    return out


def max_drawdown_pct(levels: list[tuple[str, float]], *, start: str = "", end: str = "") -> Optional[float]:
    """期間內的最大回撤（%）；從期間第一天的水準起算。沒有資料傳回 None。"""
    values = [v for d, v in levels if (not start or d >= start) and (not end or d <= end)]
    if not values:
        return None
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = max(worst, 1.0 - v / peak)
    return 100.0 * worst


def index_max_drawdown_pct(histories: dict, ex_rights: Optional[dict] = None, *, start: str = "",
                           end: str = "") -> Optional[float]:
    return max_drawdown_pct(equal_weight_levels(histories, ex_rights), start=start, end=end)

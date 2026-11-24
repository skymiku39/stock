"""因子研究：每個因子是「每檔每個評估日一個分數」，檢定分數能不能排出之後的強弱。

和 work/entry-timing/study.py 的差別：那份看「條件成立的事件」，這裡看整個橫斷面的排序，
所以可以算 IC（分數與之後報酬的排名相關）、分組報酬、因子之間的相關，並把有效的因子合成一個分數。

做法（跑之前寫死）：
- 評估日：每月 11 日（含）之後的第一個交易日。月營收法規在 10 日前公告，這天起才視為已知。
- 因子只用評估日收盤前已知的資料；季報依法定期限的隔天才視為已知（見 QUARTER_AVAILABLE）。
- 報酬：評估日的次一交易日開盤買進，第 h 個交易日收盤賣出，除權息還原；
  超額 = 報酬 − 同一天所有合格標的的平均。
- 合格標的：評估日有日 K、近 20 日平均成交金額 ≥ min_value（用當下的成交量，不是固定名單排名）。
- IC：Spearman 排名相關，每個評估日一個；t 值 = 平均 ÷ (標準差 ÷ √有效期數)，
  持有期超過一個月時有效期數依重疊比例打折。
- 通過標準（PASS，只看參數決定段、h = PRIMARY_HORIZON）：期數夠、|t| 夠、
  逐年 IC 與整體同號的年數夠、最高組減最低組的方向與 IC 相同。
- 滾動合成分數：每個評估日只用「在那之前已經結束」的 IC 選因子（同一家族取 |t| 最大的一個），
  依 IC 的正負號把排名百分位平均。它不依賴固定的參數決定段／驗證段切分。

限制：名單是 config/backtest_universe.json（2020 年成交金額最大的 60 檔），有存活者偏誤，
每組只有十檔左右；沒有歷史股本所以沒有市值因子；EPS 沒有調整股本變動。

用法：python -m bot.factor_research [--universe backtest|etf] [--root .] [--out-dir work/factor-research] [--report 路徑]
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics as st
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

TRAIN = ("2020-01-02", "2023-12-31")
VALID = ("2024-01-01", "2026-10-31")
HORIZONS = (20, 40)
PRIMARY_HORIZON = 20
GROUPS = 5
MIN_NAMES = 20                 # 一個評估日至少要有這麼多檔有分數才算 IC
MIN_VALUE_TWD = 50_000_000     # 近 20 日平均成交金額門檻
EVAL_DAY = 11
PASS = dict(min_periods=24, min_t=2.0, min_same_sign_years=3)
WF = dict(min_periods=24, min_t=2.0, lag_periods=2)  # 滾動選因子：只用 lag_periods 期以前的 IC
# 季報視為已知的日期（法定期限隔天；Q2 取金控的 8/31 期限，對其他公司偏保守）
QUARTER_AVAILABLE = {1: (0, "05-16"), 2: (0, "09-01"), 3: (0, "11-15"), 4: (1, "04-01")}


# ---------- 資料 ----------
@dataclass
class Series:
    symbol: str
    dates: list
    open: list          # 還原
    close: list         # 還原
    raw_close: list
    volume: list        # 張
    index: dict = field(default_factory=dict)

    def __post_init__(self):
        self.index = {d: i for i, d in enumerate(self.dates)}


@dataclass
class Panel:
    series: dict                       # {代號: Series}
    revenue: dict                      # {代號: [(可用日, 年增率%), ...]}
    quarterly: dict                    # {代號: [(可用日, 期別, 單季 EPS, 累計毛利率, 累計營益率), ...]}
    calendar: list = field(default_factory=list)

    def __post_init__(self):
        if not self.calendar:
            count: dict = {}
            for s in self.series.values():
                for d in s.dates:
                    count[d] = count.get(d, 0) + 1
            # 門檻取低：名單裡有很多近年才上市的標的時，早年的交易日仍要算進日曆。
            need = max(1, min(MIN_NAMES, len(self.series) // 2))
            self.calendar = sorted(d for d, n in count.items() if n >= need)


def build_series(symbol: str, bars: Sequence, events: Optional[Sequence] = None) -> Series:
    """日 K 轉成還原序列。還原因子 = 參考價 ÷ 前一日收盤，套在除權息日之前的所有日子。"""
    from bot.smile_curve_backtest import ex_rights_adjustment
    rows = [b for b in bars if all(v and v > 0 for v in (b.open, b.high, b.low, b.close))]
    dates = [str(b.date)[:10] for b in rows]
    factor = [1.0] * len(rows)
    for ev in events or []:
        adj = ex_rights_adjustment(ev)
        if not adj or not (0 < adj[1] <= 1):
            continue
        for k in range(bisect.bisect_left(dates, str(ev["ex_date"])[:10])):
            factor[k] *= adj[1]
    return Series(symbol, dates,
                  [b.open * f for b, f in zip(rows, factor)],
                  [b.close * f for b, f in zip(rows, factor)],
                  [float(b.close) for b in rows],
                  [float(b.volume or 0) for b in rows])


def quarter_available(period: str) -> Optional[str]:
    try:
        year, quarter = int(period[:4]), int(period[5])
        add, day = QUARTER_AVAILABLE[quarter]
    except (ValueError, IndexError, KeyError):
        return None
    return f"{year + add:04d}-{day}"


def single_quarter_rows(rows: Sequence) -> list:
    """[(期別, 累計 EPS, 累計毛利率, 累計營益率)] → [(可用日, 期別, 單季 EPS, 毛利率, 營益率)]。

    資料庫的 EPS 是年初至當季的累計值；Q2～Q4 的單季值要減掉同年前一季，前一季缺就不算。
    """
    cumulative = {p: eps for p, eps, *_ in rows if eps is not None}
    out = []
    for period, eps, gm, om in rows:
        available = quarter_available(str(period))
        if available is None:
            continue
        single = None
        if eps is not None:
            quarter = int(period[5])
            if quarter == 1:
                single = float(eps)
            else:
                prev = cumulative.get(f"{period[:4]}Q{quarter - 1}")
                single = float(eps) - float(prev) if prev is not None else None
        out.append((available, str(period), single, gm, om))
    out.sort()
    return out


def load_quarterly(db, symbols: Sequence[str]) -> dict:
    wanted = sorted(set(symbols))
    raw: dict = {s: [] for s in wanted}
    if wanted:
        marks = ",".join("?" * len(wanted))
        for sym, period, eps, gm, om in db.conn.execute(
                f"SELECT symbol, period, eps, gross_margin, op_margin FROM quarterly_report "
                f"WHERE symbol IN ({marks}) ORDER BY symbol, period", wanted):
            raw[sym].append((str(period), eps, gm, om))
    return {s: single_quarter_rows(rows) for s, rows in raw.items()}


UNIVERSES = {
    "backtest": "`config/backtest_universe.json`（2020 年成交金額最大的 60 檔）",
    "etf": "追蹤的主動 ETF、各 ETF 最新一期的台股持股，加上 60 檔回測名單",
}


UNIVERSE_NOTES = {
    "backtest": "- 名單只有 60 檔且有存活者偏誤，每組約十檔，分組報酬的雜訊很大。",
    "etf": "- 名單是「現在」主動 ETF 持有的股票，等於事後挑出經理人看好的標的：整份名單的報酬偏高，"
           "而且回溯到過去時，名單裡的股票當時不一定在任何 ETF 裡。因子的排序仍可比較，但數字會偏樂觀。",
}
LONG_HISTORY_BARS = 700


def universe_symbols(root: Path, universe: str) -> list:
    if universe == "etf":
        from bot.daily_collection import extended_price_symbols
        return extended_price_symbols(root)
    if universe == "backtest":
        from bot.backtest_universe import load_universe
        return load_universe(root / "config" / "backtest_universe.json")
    raise ValueError(f"unknown universe: {universe}")


def load_panel(root: Path, *, universe: str = "backtest", start: str = TRAIN[0], end: str = "") -> Panel:
    from bot.stock_db import StockDB, default_db_path
    from bot.swing_backtest import load_histories, load_revenue
    symbols = universe_symbols(root, universe)
    db = StockDB.open(path=default_db_path(root))
    try:
        histories, events = load_histories(db, symbols, start=start, end=end)
        revenue = load_revenue(db, symbols)
        quarterly = load_quarterly(db, symbols)
    finally:
        db.close()
    series = {s: build_series(s, bars, (events or {}).get(s)) for s, bars in histories.items() if bars}
    return Panel(series, revenue, quarterly)


# ---------- 因子（都只用評估日收盤前已知的資料；回傳 None 代表算不出來） ----------
def _ret(s: Series, i: int, n: int, skip: int = 0) -> Optional[float]:
    return s.close[i - skip] / s.close[i - n] - 1 if i - n >= 0 else None


def _daily_returns(s: Series, i: int, n: int) -> Optional[list]:
    if i - n < 0:
        return None
    return [s.close[k] / s.close[k - 1] - 1 for k in range(i - n + 1, i + 1)]


def _known(rows: Sequence, date: str) -> Sequence:
    return rows[:bisect.bisect_right(rows, date, key=lambda r: r[0])]


def f_low_vol(s, i, panel):
    r = _daily_returns(s, i, 60)
    return -st.pstdev(r) if r else None


def f_high52(s, i, panel):
    return s.close[i] / max(s.close[i - 249:i + 1]) if i >= 249 else None


def f_vol_surge(s, i, panel):
    if i < 119:
        return None
    long = sum(s.volume[i - 119:i + 1]) / 120
    return (sum(s.volume[i - 19:i + 1]) / 20) / long if long > 0 else None


def f_illiquidity(s, i, panel):
    r = _daily_returns(s, i, 60)
    if not r:
        return None
    vals = []
    for k, x in zip(range(i - 59, i + 1), r):
        value = s.raw_close[k] * s.volume[k] * 1000
        if value > 0:
            vals.append(abs(x) / value)
    return st.mean(vals) * 1e9 if len(vals) >= 40 else None


def _yoys(s, i, panel) -> list:
    return [y for _, y in _known(panel.revenue.get(s.symbol, []), s.dates[i])]


def f_rev_yoy(s, i, panel):
    y = _yoys(s, i, panel)
    return y[-1] if y else None


def f_rev_yoy_3m(s, i, panel):
    y = _yoys(s, i, panel)
    return st.mean(y[-3:]) if len(y) >= 3 else None


def f_rev_accel(s, i, panel):
    y = _yoys(s, i, panel)
    return y[-1] - st.mean(y[-4:-1]) if len(y) >= 4 else None


def _quarters(s, i, panel) -> Sequence:
    return _known(panel.quarterly.get(s.symbol, []), s.dates[i])


def _year_ago(rows: Sequence, period: str):
    target = f"{int(period[:4]) - 1}{period[4:]}"
    return next((r for r in reversed(rows) if r[1] == target), None)


def _consecutive(periods: Sequence[str]) -> bool:
    nums = [int(p[:4]) * 4 + int(p[5]) for p in periods]
    return all(b - a == 1 for a, b in zip(nums, nums[1:]))


def f_earnings_yield(s, i, panel):
    rows = _quarters(s, i, panel)[-4:]
    if len(rows) < 4 or any(r[2] is None for r in rows) or not _consecutive([r[1] for r in rows]):
        return None
    return sum(r[2] for r in rows) / s.raw_close[i]


def f_eps_surprise(s, i, panel):
    """最新單季 EPS 減去年同季，除以股價（基期為負時成長率沒有意義，所以不用成長率）。"""
    rows = _quarters(s, i, panel)
    if not rows or rows[-1][2] is None:
        return None
    prev = _year_ago(rows, rows[-1][1])
    return (rows[-1][2] - prev[2]) / s.raw_close[i] if prev and prev[2] is not None else None


def _margin_change(col: int):
    def fn(s, i, panel):
        rows = _quarters(s, i, panel)
        if not rows or rows[-1][col] is None:
            return None
        prev = _year_ago(rows, rows[-1][1])   # 累計值對去年同期的累計值，期間長度相同
        return rows[-1][col] - prev[col] if prev and prev[col] is not None else None
    return fn


def f_op_margin(s, i, panel):
    rows = _quarters(s, i, panel)
    return rows[-1][4] if rows else None


@dataclass(frozen=True)
class Factor:
    name: str
    family: str
    label: str
    fn: Callable


FACTORS = (
    Factor("mom_20", "momentum", "20 日漲幅", lambda s, i, p: _ret(s, i, 20)),
    Factor("mom_60", "momentum", "60 日漲幅（C2 用的相對強度）", lambda s, i, p: _ret(s, i, 60)),
    Factor("mom_120", "momentum", "120 日漲幅", lambda s, i, p: _ret(s, i, 120)),
    Factor("mom_250_skip20", "momentum", "250 日漲幅、略過最近 20 日", lambda s, i, p: _ret(s, i, 250, 20)),
    Factor("high52", "momentum", "收盤 ÷ 250 日最高收盤", f_high52),
    Factor("reversal_5", "reversal", "近 5 日跌幅（越跌分數越高）",
           lambda s, i, p: None if _ret(s, i, 5) is None else -_ret(s, i, 5)),
    Factor("low_vol_60", "risk", "60 日波動度（越低分數越高）", f_low_vol),
    Factor("vol_surge", "liquidity", "20 日均量 ÷ 120 日均量", f_vol_surge),
    Factor("illiquidity_60", "liquidity", "Amihud 非流動性（60 日）", f_illiquidity),
    Factor("rev_yoy", "revenue", "最新月營收年增率", f_rev_yoy),
    Factor("rev_yoy_3m", "revenue", "近 3 個月營收年增率平均", f_rev_yoy_3m),
    Factor("rev_accel", "revenue", "營收年增率 − 前 3 個月平均", f_rev_accel),
    Factor("earnings_yield", "value", "近四季 EPS ÷ 股價", f_earnings_yield),
    Factor("eps_surprise", "earnings", "單季 EPS 年增 ÷ 股價", f_eps_surprise),
    Factor("gross_margin_chg", "quality", "累計毛利率 − 去年同期", _margin_change(3)),
    Factor("op_margin_chg", "quality", "累計營益率 − 去年同期", _margin_change(4)),
    Factor("op_margin", "quality", "累計營益率", f_op_margin),
)


# ---------- 統計 ----------
def ranks(values: Sequence[float]) -> list:
    order = sorted(range(len(values)), key=lambda k: values[k])
    out = [0.0] * len(values)
    a = 0
    while a < len(order):
        b = a
        while b + 1 < len(order) and values[order[b + 1]] == values[order[a]]:
            b += 1
        for k in range(a, b + 1):
            out[order[k]] = (a + b) / 2 + 1
        a = b + 1
    return out


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = st.mean(rx), st.mean(ry)
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy)


def t_stat(values: Sequence[float], horizon: int = PRIMARY_HORIZON) -> Optional[float]:
    n = len(values)
    if n < 3:
        return None
    sd = st.stdev(values)
    if sd == 0:
        return None
    effective = n * min(1.0, 21 / horizon)
    return st.mean(values) / (sd / math.sqrt(effective))


# ---------- 評估 ----------
def eval_dates(calendar: Sequence[str], *, day: int = EVAL_DAY) -> list:
    out, seen = [], set()
    for d in calendar:
        if d[:7] not in seen and int(d[8:10]) >= day:
            seen.add(d[:7])
            out.append(d)
    return out


def eligible(s: Series, i: int, min_value: float) -> bool:
    if i < 19:
        return False
    return sum(s.raw_close[k] * s.volume[k] * 1000 for k in range(i - 19, i + 1)) / 20 >= min_value


def forward_excess(panel: Panel, date: str, horizon: int, symbols: Sequence[str]) -> tuple:
    """({代號: 超額報酬}, 視窗結束日)。停牌太久、第 h 根日 K 比市場晚超過 5 個交易日的不算。"""
    ci = bisect.bisect_left(panel.calendar, date)
    limit = panel.calendar[min(ci + horizon + 5, len(panel.calendar) - 1)]
    raw, end = {}, ""
    for sym in symbols:
        s = panel.series[sym]
        i = s.index[date]
        if i + horizon >= len(s.dates) or s.dates[i + horizon] > limit:
            continue
        raw[sym] = s.close[i + horizon] / s.open[i + 1] - 1
        end = max(end, s.dates[i + horizon])
    if not raw:
        return {}, ""
    mean = st.mean(raw.values())
    return {sym: r - mean for sym, r in raw.items()}, end


def group_means(scores: dict, excess: dict, groups: int = GROUPS) -> Optional[list]:
    syms = sorted((s for s in scores if s in excess), key=lambda s: (scores[s], s))
    if len(syms) < groups * 2:
        return None
    buckets: list = [[] for _ in range(groups)]
    for k, sym in enumerate(syms):
        buckets[k * groups // len(syms)].append(excess[sym])
    return [st.mean(b) for b in buckets]


def score_dates(panel: Panel, factors: Sequence[Factor], *, min_value: float = MIN_VALUE_TWD) -> dict:
    """{評估日: {因子名: {代號: 分數}}}，只含當天合格的標的。"""
    out = {}
    for date in eval_dates(panel.calendar):
        pool = [(sym, s, s.index[date]) for sym, s in panel.series.items()
                if date in s.index and eligible(s, s.index[date], min_value)]
        day = {}
        for f in factors:
            scores = {}
            for sym, s, i in pool:
                value = f.fn(s, i, panel)
                if value is not None and math.isfinite(value):
                    scores[sym] = float(value)
            day[f.name] = scores
        out[date] = {"pool": [sym for sym, _, _ in pool], "scores": day}
    return out


def observe(scores: dict, excess: dict, *, min_names: int = MIN_NAMES) -> Optional[dict]:
    syms = [s for s in scores if s in excess]
    if len(syms) < min_names:
        return None
    ic = spearman([scores[s] for s in syms], [excess[s] for s in syms])
    if ic is None:
        return None
    return {"ic": ic, "n": len(syms), "groups": group_means(scores, excess)}


def percentile_ranks(scores: dict) -> dict:
    syms = list(scores)
    r = ranks([scores[s] for s in syms])
    n = len(syms)
    return {s: (x - 1) / (n - 1) if n > 1 else 0.5 for s, x in zip(syms, r)}


def summarize(obs: Sequence[tuple], horizon: int) -> dict:
    """obs = [(評估日, observe 的結果)]。"""
    ics = [o["ic"] for _, o in obs]
    out: dict = {"periods": len(ics)}
    if not ics:
        return out
    out["ic_mean"] = st.mean(ics)
    out["ic_t"] = t_stat(ics, horizon)
    out["ic_positive"] = sum(x > 0 for x in ics) / len(ics)
    out["names"] = st.mean(o["n"] for _, o in obs)
    years: dict = {}
    for date, o in obs:
        years.setdefault(date[:4], []).append(o["ic"])
    out["by_year"] = {y: st.mean(v) for y, v in sorted(years.items())}
    grouped = [o["groups"] for _, o in obs if o["groups"]]
    if grouped:
        out["groups"] = [st.mean(g[k] for g in grouped) for k in range(len(grouped[0]))]
        spreads = [g[-1] - g[0] for g in grouped]
        out["spread_mean"] = st.mean(spreads)
        out["spread_t"] = t_stat(spreads, horizon)
    return out


def passes(summary: dict, rule: dict = PASS) -> bool:
    t, mean = summary.get("ic_t"), summary.get("ic_mean")
    if t is None or summary.get("periods", 0) < rule["min_periods"] or abs(t) < rule["min_t"]:
        return False
    same = sum(1 for v in summary.get("by_year", {}).values() if v * mean > 0)
    spread = summary.get("spread_mean")
    return same >= rule["min_same_sign_years"] and spread is not None and spread * mean > 0


def _segment(date: str, end: str) -> Optional[str]:
    for name, (lo, hi) in (("train", TRAIN), ("valid", VALID)):
        if lo <= date <= hi and end <= hi:
            return name
    return None


def walk_forward_scores(dates: Sequence[str], scored: dict, ic_by_factor: dict,
                        factors: Sequence[Factor], rule: dict = WF) -> tuple:
    """每個評估日只用 lag_periods 期以前的 IC 選因子，回傳 ({日期: {代號: 合成分數}}, {日期: [(因子, 正負號)]})。"""
    family = {f.name: f.family for f in factors}
    composite, chosen = {}, {}
    for k, date in enumerate(dates):
        past = set(dates[:max(0, k - rule["lag_periods"] + 1)])
        best: dict = {}
        for name, ics in ic_by_factor.items():
            history = [v for d, v in ics.items() if d in past]
            t = t_stat(history)
            if t is None or len(history) < rule["min_periods"] or abs(t) < rule["min_t"]:
                continue
            if family[name] not in best or abs(t) > abs(best[family[name]][1]):
                best[family[name]] = (name, t)
        picks = sorted((name, 1 if t > 0 else -1) for name, t in best.values())
        if not picks:
            continue
        chosen[date] = picks
        parts: dict = {}
        for name, sign in picks:
            for sym, pct in percentile_ranks(scored[date]["scores"][name]).items():
                parts.setdefault(sym, []).append(pct if sign > 0 else 1 - pct)
        need = math.ceil(len(picks) / 2)
        composite[date] = {sym: st.mean(v) for sym, v in parts.items() if len(v) >= need}
    return composite, chosen


def factor_correlation(dates: Sequence[str], scored: dict, names: Sequence[str]) -> dict:
    out = {}
    for a_i, a in enumerate(names):
        for b in names[a_i + 1:]:
            vals = []
            for date in dates:
                sa, sb = scored[date]["scores"][a], scored[date]["scores"][b]
                syms = [s for s in sa if s in sb]
                if len(syms) >= MIN_NAMES:
                    rho = spearman([sa[s] for s in syms], [sb[s] for s in syms])
                    if rho is not None:
                        vals.append(rho)
            if vals:
                out[f"{a}|{b}"] = st.mean(vals)
    return out


def evaluate(panel: Panel, factors: Sequence[Factor] = FACTORS, *, horizons: Sequence[int] = HORIZONS,
             min_value: float = MIN_VALUE_TWD) -> dict:
    scored = score_dates(panel, factors, min_value=min_value)
    dates = sorted(scored)
    names = [f.name for f in factors]
    result: dict = {"factors": {}, "horizons": list(horizons), "eval_dates": len(dates),
                    "first_date": dates[0] if dates else "", "last_date": dates[-1] if dates else ""}
    forward = {h: {d: forward_excess(panel, d, h, scored[d]["pool"]) for d in dates} for h in horizons}

    def run(score_of: Callable, h: int) -> dict:
        obs = {"train": [], "valid": [], "all": []}
        for d in dates:
            excess, end = forward[h][d]
            o = observe(score_of(d), excess) if excess else None
            if o is None:
                continue
            obs["all"].append((d, o))
            seg = _segment(d, end)
            if seg:
                obs[seg].append((d, o))
        return obs

    primary_ic: dict = {}
    for f in factors:
        entry: dict = {"family": f.family, "label": f.label, "horizons": {}}
        for h in horizons:
            obs = run(lambda d, n=f.name: scored[d]["scores"][n], h)
            entry["horizons"][str(h)] = {seg: summarize(v, h) for seg, v in obs.items()}
            if h == PRIMARY_HORIZON:
                primary_ic[f.name] = {d: o["ic"] for d, o in obs["all"]}
        entry["pass"] = passes(entry["horizons"][str(PRIMARY_HORIZON)]["train"])
        result["factors"][f.name] = entry

    composite, chosen = walk_forward_scores(dates, scored, primary_ic, factors)
    wf: dict = {"horizons": {}, "first_date": min(composite) if composite else "",
                "selection": {d: [f"{'+' if s > 0 else '-'}{n}" for n, s in picks] for d, picks in chosen.items()}}
    for h in horizons:
        obs = run(lambda d: composite.get(d, {}), h)
        wf["horizons"][str(h)] = {seg: summarize(v, h) for seg, v in obs.items()}
    result["walk_forward"] = wf
    train_dates = [d for d in dates if TRAIN[0] <= d <= TRAIN[1]]
    result["correlation_train"] = factor_correlation(train_dates, scored, names)
    result["config"] = {"train": TRAIN, "valid": VALID, "groups": GROUPS, "min_names": MIN_NAMES,
                        "min_value_twd": min_value, "eval_day": EVAL_DAY, "pass": PASS, "walk_forward": WF,
                        "symbols": len(panel.series),
                        "long_history": sum(len(s.dates) >= LONG_HISTORY_BARS for s in panel.series.values())}
    pools = [len(scored[d]["pool"]) for d in dates]
    if pools:
        result["config"].update(pool_mean=st.mean(pools), pool_min=min(pools), pool_max=max(pools))
    return result


# ---------- 報告 ----------
def _f(value, digits: int = 3, pct: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.2f}%" if pct else f"{value:+.{digits}f}"


def _years(summary: dict) -> str:
    return "、".join(f"{y[2:]}:{v:+.2f}" for y, v in summary.get("by_year", {}).items()) or "—"


def render(result: dict) -> str:
    cfg, h = result["config"], str(PRIMARY_HORIZON)
    lines = [
        "# 因子研究", "",
        f"- 名單：{UNIVERSES.get(cfg.get('universe', 'backtest'), cfg.get('universe'))}，有日 K 的 {cfg['symbols']} 檔，"
        f"其中 {cfg.get('long_history', '—')} 檔有 {LONG_HISTORY_BARS} 根以上；"
        f"每個評估日平均 {cfg.get('pool_mean', 0):.0f} 檔合格（最少 {cfg.get('pool_min', 0)}、最多 {cfg.get('pool_max', 0)}）",
        f"- 評估日 {result['eval_dates']} 個（{result['first_date']}～{result['last_date']}，"
        f"每月 {cfg['eval_day']} 日後第一個交易日）",
        f"- 參數決定段 {cfg['train'][0]}～{cfg['train'][1]}，驗證段 {cfg['valid'][0]}～{cfg['valid'][1]}",
        f"- 合格標的：近 20 日平均成交金額 ≥ {cfg['min_value_twd'] / 1e8:.1f} 億元；分 {cfg['groups']} 組",
        f"- 通過標準（只看參數決定段、{h} 日）：期數 ≥ {cfg['pass']['min_periods']}、|t| ≥ {cfg['pass']['min_t']}、"
        f"逐年同號 ≥ {cfg['pass']['min_same_sign_years']} 年、最高組減最低組與 IC 同向",
        "", f"## 各因子（{h} 日超額報酬）", "",
        "| 因子 | 說明 | 決定段 IC | t | 期數 | 逐年 IC | 高減低 | 驗證段 IC | t | 高減低 | 通過 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    ordered = sorted(result["factors"].items(),
                     key=lambda kv: -abs(kv[1]["horizons"][h]["train"].get("ic_t") or 0))
    for name, e in ordered:
        tr, va = e["horizons"][h]["train"], e["horizons"][h]["valid"]
        lines.append(f"| `{name}` | {e['label']} | {_f(tr.get('ic_mean'))} | {_f(tr.get('ic_t'), 2)} | "
                     f"{tr.get('periods', 0)} | {_years(tr)} | {_f(tr.get('spread_mean'), pct=True)} | "
                     f"{_f(va.get('ic_mean'))} | {_f(va.get('ic_t'), 2)} | {_f(va.get('spread_mean'), pct=True)} | "
                     f"{'**是**' if e['pass'] else '否'} |")
    other = [x for x in result["horizons"] if str(x) != h]
    for x in other:
        lines += ["", f"## {x} 日超額報酬（參考）", "",
                  "| 因子 | 決定段 IC | t | 高減低 | 驗證段 IC | t | 高減低 |", "|---|---|---|---|---|---|---|"]
        for name, e in ordered:
            tr, va = e["horizons"][str(x)]["train"], e["horizons"][str(x)]["valid"]
            lines.append(f"| `{name}` | {_f(tr.get('ic_mean'))} | {_f(tr.get('ic_t'), 2)} | "
                         f"{_f(tr.get('spread_mean'), pct=True)} | {_f(va.get('ic_mean'))} | "
                         f"{_f(va.get('ic_t'), 2)} | {_f(va.get('spread_mean'), pct=True)} |")
    lines += ["", "## 分組報酬（通過的因子，決定段／驗證段，由低分組到高分組）", ""]
    passed = [(n, e) for n, e in ordered if e["pass"]]
    if not passed:
        lines.append("沒有因子通過。")
    for name, e in passed:
        for seg, title in (("train", "決定段"), ("valid", "驗證段")):
            groups = e["horizons"][h][seg].get("groups")
            if groups:
                lines.append(f"- `{name}` {title}：" + "、".join(_f(g, pct=True) for g in groups))
    wf = result["walk_forward"]
    lines += ["", "## 滾動合成分數", ""]
    if not wf["first_date"]:
        lines.append("沒有任何評估日選得出因子。")
    else:
        lines.append(f"每個評估日只用更早的 IC 選因子（期數 ≥ {cfg['walk_forward']['min_periods']}、"
                     f"|t| ≥ {cfg['walk_forward']['min_t']}、同家族取一個），第一次選得出因子是 {wf['first_date']}。")
        lines += ["", "| 持有 | 期數 | IC | t | IC > 0 | 逐年 IC | 高減低 | t |", "|---|---|---|---|---|---|---|---|"]
        for x in result["horizons"]:
            s = wf["horizons"][str(x)]["all"]
            pos = s.get("ic_positive")
            lines.append(f"| {x} 日 | {s.get('periods', 0)} | {_f(s.get('ic_mean'))} | {_f(s.get('ic_t'), 2)} | "
                         f"{'—' if pos is None else f'{pos:.0%}'} | {_years(s)} | "
                         f"{_f(s.get('spread_mean'), pct=True)} | {_f(s.get('spread_t'), 2)} |")
        counts: dict = {}
        for picks in wf["selection"].values():
            for p in picks:
                counts[p] = counts.get(p, 0) + 1
        lines += ["", "被選中的次數：" + "、".join(f"`{k}` {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))]
    high = sorted(((k, v) for k, v in result["correlation_train"].items() if abs(v) >= 0.5), key=lambda kv: -abs(kv[1]))
    lines += ["", "## 因子之間的相關（決定段，|相關| ≥ 0.5）", ""]
    lines += [f"- `{k.replace('|', '` 與 `')}`：{v:+.2f}" for k, v in high] or ["沒有。"]
    lines += ["", "## 怎麼讀", "",
              "- IC 是分數排名與之後超額報酬排名的相關，月頻 IC 平均 0.03～0.05 就算有用；看 t 值與逐年是否同號，不要只看平均。",
              f"- 一次檢定 {len(result['factors'])} 個因子，|t| ≥ 2 本來就會有大約一個是運氣。",
              UNIVERSE_NOTES.get(cfg.get("universe", "backtest"), ""),
              "- 動能與營收在先前的研究已經看過驗證段，那兩類在驗證段的數字只算弱證據。",
              "- 滾動合成分數不依賴固定切分，但前幾年的資料只夠拿來選因子，實際檢驗的期數不多。", ""]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from bot.cli_encoding import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description="因子研究：IC、分組報酬、滾動合成分數")
    parser.add_argument("--root", default=".")
    parser.add_argument("--universe", choices=sorted(UNIVERSES), default="backtest")
    parser.add_argument("--out-dir", default="work/factor-research")
    parser.add_argument("--report", default="", help="預設 docs/reports/factor-research[-名單].md")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    suffix = "" if args.universe == "backtest" else f"-{args.universe}"
    result = evaluate(load_panel(root, universe=args.universe))
    result["config"]["universe"] = args.universe
    out = root / args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / f"results{suffix}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    text = render(result)
    report = args.report or f"docs/reports/factor-research{suffix}.md"
    (root / report).write_text(text, encoding="utf-8", newline="\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

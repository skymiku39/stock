"""random_entry -- 隨機進場基準（#30）：只把進場時機換成隨機，其他規則不變。

用途：判斷策略的進場規則有沒有貢獻。同一批標的、同樣的出場規則、部位大小、資金與成交假設，
進場改成每天以固定機率觸發，重複多次取分布。策略的每輪期望值若沒有高於這個分布的
第 95 百分位，代表獲利不是進場規則帶來的。

進場機率取自策略自己的回測：各階「成交買進次數 ÷ 標的交易日數」，再等比例校準到隨機版本的
平均成交買進次數與策略相近（校準結果與實際成交次數都列在輸出，供核對）。

與策略無關的部分（亂數、重複執行、分布統計）放在這裡，微笑方案與波段原型共用。
"""

from __future__ import annotations

import random
from typing import Callable, Hashable, Mapping, Optional, Sequence

from bot.round_metrics import account_metrics, summarize_rounds

DEFAULT_RUNS = 200
DEFAULT_SEED = 20261116


class RandomEntry:
    """一次執行用的隨機進場判定。相同 seed 與相同呼叫順序會得到相同結果。"""

    def __init__(self, rates: Mapping[Hashable, float], seed: str):
        self.rates = dict(rates)
        self._rng = random.Random(str(seed))  # 字串種子以 SHA-512 展開，跨平台一致

    def fires(self, key: Hashable) -> bool:
        # 不論機率多少都抽一次，呼叫順序固定時亂數序列才不會因機率設定而錯位。
        return self._rng.random() < self.rates.get(key, 0.0)


def percentile(values: Sequence[float], q: float) -> float:
    """線性內插百分位，q 為 0–100。"""
    data = sorted(values)
    if not data:
        raise ValueError("percentile of empty data")
    pos = (len(data) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def summarize_distribution(samples: Sequence[Mapping[str, object]]) -> dict:
    """每個數值指標 → n、平均、第 5／50／95 百分位。None（例如沒有虧損時的獲利因子）不計。"""
    keys: list[str] = []
    for sample in samples:
        for key in sample:
            if key not in keys:
                keys.append(key)
    out: dict = {}
    for key in keys:
        values = [float(s[key]) for s in samples
                  if isinstance(s.get(key), (int, float)) and not isinstance(s.get(key), bool)]
        if not values:
            continue
        out[key] = dict(n=len(values), mean=sum(values) / len(values),
                        p5=percentile(values, 5), p50=percentile(values, 50),
                        p95=percentile(values, 95))
    return out


def calibrate_rates(count_entries: Callable[[RandomEntry], float],
                    rates: Mapping[Hashable, float], target: float, *, seed: int = DEFAULT_SEED,
                    pilots: int = 10, max_steps: int = 10, tolerance: float = 0.1) -> tuple:
    """等比例放大或縮小各機率，讓隨機版本的平均進場次數接近 target。

    為什麼需要：原始機率是「成交次數 ÷ 交易日數」，但持倉中同一階不會重複買、停用後也不買，
    實際能抽籤的日子比較少，直接用會讓進場次數遠低於策略。校準用獨立的種子，不影響正式抽樣。
    回傳 (校準後機率, 說明)；說明含倍率、試跑的平均進場次數、是否落在容許範圍內。
    """
    scale, scaled, mean = 1.0, dict(rates), 0.0
    info = dict(target=target, scale=1.0, pilot_mean=None, within_tolerance=False, steps=0)
    if target <= 0 or not rates:
        return scaled, info
    for step in range(max_steps):
        scaled = {k: min(1.0, p * scale) for k, p in rates.items()}
        mean = sum(count_entries(RandomEntry(scaled, f"{seed}:calibrate:{step}:{i}"))
                   for i in range(pilots)) / pilots
        info.update(scale=scale, pilot_mean=mean, steps=step + 1,
                    within_tolerance=mean > 0 and abs(target / mean - 1.0) <= tolerance)
        if info["within_tolerance"]:
            break
        scale *= target / mean if mean > 0 else 2.0
    return scaled, info


def run_baseline(run_once: Callable[[RandomEntry], Mapping[str, object]],
                 rates: Mapping[Hashable, float], *, runs: int = DEFAULT_RUNS,
                 seed: int = DEFAULT_SEED) -> dict:
    """重複執行 run_once（每次給一個獨立種子的 RandomEntry），回傳各指標的分布。"""
    if runs <= 0:
        raise ValueError("runs must be positive")
    samples = [dict(run_once(RandomEntry(rates, f"{seed}:{i}"))) for i in range(runs)]
    return dict(runs=runs, seed=seed,
                rates={(k if isinstance(k, str) else f"tier{k}"): v for k, v in rates.items()},
                distribution=summarize_distribution(samples))


# ── 微笑方案 ──────────────────────────────────────────────────────────────

def flat_metrics(summary) -> dict:
    """回測摘要（微笑或波段）→ 單層指標（一輪指標、帳戶指標、進場次數）。"""
    out = {k: v for k, v in summarize_rounds(summary.rounds).items() if k != "exit_reason_counts"}
    out.update(account_metrics(summary.equity_curve, summary.initial_cash,
                               summary.buy_cost_total_twd))
    out.update(net_pnl_twd=summary.net_pnl_twd, return_pct=summary.return_pct,
               filled_buys=sum(1 for o in summary.orders
                               if o["side"] == "buy" and o["status"] == "filled"))
    return out


def smile_entry_rates(summary) -> dict:
    """各階的每日進場機率 = 該階成交買進次數 ÷ 全部標的的日 K 根數。"""
    days = sum(r.bar_count for r in summary.results)
    if days <= 0:
        return {}
    counts: dict = {}
    for order in summary.orders:
        if order["side"] == "buy" and order["status"] == "filled":
            key = "bottom" if order.get("tier") is None else order["tier"]
            counts[key] = counts.get(key, 0) + 1
    return {key: n / days for key, n in sorted(counts.items(), key=lambda kv: str(kv[0]))}


def strategy_random_baseline(run: Callable[[Optional[RandomEntry]], object],
                             rates_of: Callable[[object], Mapping[Hashable, float]], *,
                             runs: int = DEFAULT_RUNS, seed: int = DEFAULT_SEED) -> dict:
    """任何策略共用：run(None) 跑策略本身，run(RandomEntry) 跑一次隨機進場。

    先用策略自己的成交次數算出進場機率並校準，再跑 runs 次取分布。
    """
    strategy = run(None)
    metrics = flat_metrics(strategy)

    def once(entry: RandomEntry) -> dict:
        return flat_metrics(run(entry))

    rates, calibration = calibrate_rates(lambda entry: once(entry)["filled_buys"],
                                         rates_of(strategy), metrics["filled_buys"], seed=seed)
    baseline = run_baseline(once, rates, runs=runs, seed=seed)
    baseline["calibration"] = calibration
    spread = baseline["distribution"].get("expectancy_pct")
    return dict(strategy=metrics, random_entry=baseline,
                expectancy_pct_above_p95=(bool(metrics["expectancy_pct"] > spread["p95"])
                                          if spread else None))


def smile_random_baseline(backtester, histories: dict, *, ex_rights: Optional[dict] = None,
                          start: str = "", end: str = "", runs: int = DEFAULT_RUNS,
                          seed: int = DEFAULT_SEED) -> dict:
    """微笑方案：各階不看跌幅，改以機率觸發。"""
    return strategy_random_baseline(
        lambda entry: backtester.run_histories(histories, start=start, end=end,
                                               ex_rights=ex_rights, random_entry=entry),
        smile_entry_rates, runs=runs, seed=seed)


def entry_rate(summary, histories: dict) -> dict:
    """單一進場訊號的策略（波段原型）：每日進場機率 = 成交買進次數 ÷ 全部標的的日 K 根數。"""
    days = sum(len(bars) for bars in histories.values())
    fills = sum(1 for o in summary.orders if o["side"] == "buy" and o["status"] == "filled")
    return {"entry": fills / days} if days > 0 and fills else {}


def swing_random_baseline(backtester, histories: dict, *, ex_rights: Optional[dict] = None,
                          trade_start: str = "", runs: int = DEFAULT_RUNS,
                          seed: int = DEFAULT_SEED) -> dict:
    """波段原型：不看突破與均線，改以機率觸發；停損、移動停利、期限與部位大小不變。"""
    return strategy_random_baseline(
        lambda entry: backtester.run_histories(histories, trade_start=trade_start,
                                               ex_rights=ex_rights, random_entry=entry),
        lambda summary: entry_rate(summary, histories), runs=runs, seed=seed)

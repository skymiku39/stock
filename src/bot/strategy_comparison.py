"""strategy_comparison -- 改造後的微笑方案與波段原型的比較（#19）。只讀資料庫、只跑回測。

    uv run --offline --no-sync python -m bot.strategy_comparison \\
        --output docs/reports/strategy-comparison.md --json work/strategy-comparison/result.json

事先固定的內容（改了就不是同一份比較）：
- 標的：config/backtest_universe.json 的 60 檔（2020 年成交金額排名，排除 2330）。
- 期間：2020-01-02～2023-12-31 決定參數；2024-01-01～2026-10-31 只驗證一次。
- 參數組合：兩種策略各 9 組（下方 SMILE_GRID、SWING_GRID），報告列出全部。
- 選定規則：參數決定段中，自己與所有相鄰組合的每輪期望值都 > 0 的組合裡，取每輪期望值最高者；
  沒有任何組合符合時，取期望值最高者並標示「相鄰組合未全數為正」。
- 通過標準：docs/swing-strategy-design.md 四之 5（核心條件四項、參考值兩項）。
- 亂數：隨機進場 200 次、重抽樣 1,000 次，種子固定。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from bot.backtest_universe import load_universe
from bot.config import Settings
from bot.random_entry import (DEFAULT_SEED, flat_metrics, percentile, smile_random_baseline,
                              swing_random_baseline)
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.smile_execution import SmileExecution
from bot.stock_db import StockDB, default_db_path
from bot.swing_backtest import SwingBacktester, SwingParams, load_histories

TRAIN = ("2020-01-02", "2023-12-31")
VALIDATION = ("2024-01-01", "2026-10-31")
SWING_WARMUP_START = "2023-07-01"  # 驗證段的波段指標需要先暖機，這段不產生訊號
INITIAL_CASH = 500_000
BOOTSTRAP_RUNS = 1000
RANDOM_RUNS = 200
MAX_DRAWDOWN_PCT = 20.0
REFERENCE_ROUNDS = 60
REFERENCE_HOLD_DAYS = (14, 90)

# 兩種策略共用：費率、零股、成交假設。明確寫出，不讀 .env，結果才可重現。
COMMON = dict(_env_file=None, broker_fee_discount=0.28, broker_min_fee=1.0, use_odd_lot=True)
EXECUTION = dict(slippage_bps=10.0, participation=0.001)

# 微笑方案：#31、#32 的暫定值固定，比較停利與單檔停損。MAX_FUND 設成遠高於起始資金的硬上限。
SMILE_FIXED = dict(max_fund=10_000_000, daily_fund_budget=0, max_lot_per_symbol=1000,
                   smile_initial_fund=INITIAL_CASH, smile_unit_equity_pct=3.3,
                   smile_max_symbol_fund_pct=20.0, smile_buy_tiers="3:1,6:2,10:3",
                   smile_min_first_tier_pct=3.0, smile_bottom_dca_enabled=False,
                   smile_max_hold_days=90, smile_reentry_cooldown_days=20,
                   smile_trailing_reference=True, smile_max_unrealized_loss_pct=20.0,
                   smile_regular_tax_rate=0.003)
SMILE_AXES = ("smile_take_profit_pct", (0.0, 5.0, 8.0)), ("smile_symbol_stop_loss_pct", (10.0, 15.0, 25.0))
# 波段原型：其餘參數用 SwingParams 的預設值，比較突破天數與移動停利倍數。
SWING_AXES = ("breakout_days", (20, 40, 60)), ("trail_atr", (2.0, 3.0, 4.0))


def grid(axes) -> list[dict]:
    (name_a, values_a), (name_b, values_b) = axes
    return [dict(index=(i, j), params={name_a: a, name_b: b})
            for i, a in enumerate(values_a) for j, b in enumerate(values_b)]


def neighbours(index: tuple, combos: Sequence[dict]) -> list[dict]:
    i, j = index
    return [c for c in combos if abs(c["index"][0] - i) + abs(c["index"][1] - j) == 1]


def choose(combos: Sequence[dict]) -> dict:
    """依事先訂好的規則選定參數；combos 每筆已含 train 指標。"""
    def expectancy(c):
        return c["train"]["expectancy_pct"]
    robust = [c for c in combos if c["train"]["round_count"] > 0 and expectancy(c) > 0
              and all(expectancy(n) > 0 for n in neighbours(c["index"], combos))]
    pool = robust or [c for c in combos if c["train"]["round_count"] > 0] or list(combos)
    best = max(pool, key=lambda c: (expectancy(c), tuple(-x for x in c["index"])))
    return dict(index=best["index"], params=best["params"], neighbours_all_positive=bool(robust))


def yearly_pnl(equity_curve: Sequence[dict], initial_cash: float) -> dict:
    """每個日曆年的帳戶淨損益（含未實現）：該年最後一筆權益 − 前一年最後一筆權益。"""
    last_by_year: dict = {}
    for point in equity_curve:
        last_by_year[point["date"][:4]] = float(point["equity"])
    out, previous = {}, float(initial_cash)
    for year in sorted(last_by_year):
        out[year] = last_by_year[year] - previous
        previous = last_by_year[year]
    return out


def bootstrap_p5(values: Sequence[float], *, runs: int = BOOTSTRAP_RUNS,
                 seed: int = DEFAULT_SEED) -> Optional[float]:
    """對各輪報酬率重抽樣（取後放回），回傳平均值分布的第 5 百分位。"""
    values = list(values)
    if not values:
        return None
    rng = random.Random(f"{seed}:bootstrap")
    n = len(values)
    means = [sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(runs)]
    return percentile(means, 5)


def by_symbol(rounds: Sequence) -> list[dict]:
    totals: dict = {}
    for r in rounds:
        row = totals.setdefault(r.symbol, dict(symbol=r.symbol, rounds=0, pnl_twd=0.0))
        row["rounds"] += 1
        row["pnl_twd"] += r.pnl_twd
    return sorted(totals.values(), key=lambda row: (row["pnl_twd"], row["symbol"]))


def describe(summary, *, year_from: str = "") -> dict:
    """單次回測 → 指標、逐年損益、最差標的、最差的輪次。"""
    metrics = flat_metrics(summary)
    reasons: dict = {}
    for r in summary.rounds:
        reasons[r.exit_reason] = reasons.get(r.exit_reason, 0) + 1
    curve = [p for p in summary.equity_curve if p["date"] >= year_from] if year_from else summary.equity_curve
    losing = sorted((r for r in summary.rounds if r.pnl_twd < 0), key=lambda r: r.pnl_twd)
    symbols = by_symbol(summary.rounds)
    return dict(metrics=metrics, exit_reasons=reasons,
                yearly_pnl_twd=yearly_pnl(curve, summary.initial_cash),
                losing_round_count=len(losing),
                losing_rounds_pnl_twd=sum(r.pnl_twd for r in losing),
                worst_rounds=[r.to_dict() for r in losing[:10]],
                worst_symbols=symbols[:5], best_symbols=symbols[-5:][::-1],
                open_positions=_open_count(summary),
                data_ended=list(getattr(summary, "data_ended", [])),
                halted_date=getattr(summary, "halted_date", None))


def _open_count(summary) -> int:
    if hasattr(summary, "results"):
        return sum(1 for r in summary.results if r.open_lots > 0)
    return len(summary.open_positions)


def judge(validation: dict, bootstrap: Optional[float], random_p95: Optional[float]) -> dict:
    m = validation["metrics"]
    years = validation["yearly_pnl_twd"]
    core = [
        dict(name="每輪期望值 > 0", value=m["expectancy_pct"], passed=m["round_count"] > 0 and m["expectancy_pct"] > 0),
        dict(name="重抽樣 1,000 次的第 5 百分位 > 0", value=bootstrap,
             passed=bootstrap is not None and bootstrap > 0),
        dict(name="驗證段每個日曆年的淨損益都 > 0", value=years,
             passed=bool(years) and all(v > 0 for v in years.values())),
        dict(name="每輪期望值高於隨機進場第 95 百分位", value=dict(strategy=m["expectancy_pct"], random_p95=random_p95),
             passed=random_p95 is not None and m["round_count"] > 0 and m["expectancy_pct"] > random_p95),
        dict(name=f"最大回撤 ≤ {MAX_DRAWDOWN_PCT:.0f}%", value=m["max_drawdown_pct"],
             passed=m["max_drawdown_pct"] <= MAX_DRAWDOWN_PCT),
    ]
    low, high = REFERENCE_HOLD_DAYS
    reference = [
        dict(name=f"平倉輪數（參考值 {REFERENCE_ROUNDS}）", value=m["round_count"],
             met=m["round_count"] >= REFERENCE_ROUNDS,
             note="" if m["round_count"] >= REFERENCE_ROUNDS else "樣本不足，屬初步證據"),
        dict(name=f"平均持有天數（參考範圍 {low}～{high}）", value=m["hold_days_mean"],
             met=low <= m["hold_days_mean"] <= high,
             note="" if low <= m["hold_days_mean"] <= high else "不符合短中期定位"),
    ]
    return dict(core=core, reference=reference, has_evidence=all(c["passed"] for c in core))


def _smile_backtester(params: dict) -> SmileCurveBacktester:
    settings = Settings(**{**COMMON, **SMILE_FIXED, **params})
    return SmileCurveBacktester(settings, execution=SmileExecution(**EXECUTION))


def _swing_backtester(params: dict) -> SwingBacktester:
    return SwingBacktester(Settings(**COMMON), SwingParams(**params),
                           execution=SmileExecution(**EXECUTION), initial_cash=INITIAL_CASH)


def evaluate(name: str, axes, run_train: Callable[[dict], object],
             run_validation: Callable[[dict], object],
             random_baseline: Callable[[dict], dict], *, validate: bool, log) -> dict:
    combos = grid(axes)
    for combo in combos:
        summary = run_train(combo["params"])
        combo["train"] = flat_metrics(summary)
        combo["train_yearly_pnl_twd"] = yearly_pnl(summary.equity_curve, summary.initial_cash)
        log(f"[{name}] train {combo['params']}: rounds={combo['train']['round_count']} "
            f"expectancy={combo['train']['expectancy_pct']:.2f}%")
    chosen = choose(combos)
    for combo in combos:
        combo["chosen"] = combo["index"] == chosen["index"]
        combo["index"] = list(combo["index"])
    result = dict(name=name, axes=[dict(name=a, values=list(v)) for a, v in axes],
                  combos=combos, chosen=dict(chosen, index=list(chosen["index"])))
    if not validate:
        return result
    log(f"[{name}] validation with {chosen['params']} (run once)")
    summary = run_validation(chosen["params"])
    validation = describe(summary, year_from=VALIDATION[0])
    bootstrap = bootstrap_p5([r.return_pct for r in summary.rounds])
    log(f"[{name}] random-entry baseline x{RANDOM_RUNS}")
    baseline = random_baseline(chosen["params"])
    spread = baseline["random_entry"]["distribution"].get("expectancy_pct")
    result.update(validation=validation, bootstrap_p5_expectancy_pct=bootstrap,
                  random_entry=baseline["random_entry"],
                  verdict=judge(validation, bootstrap, spread["p95"] if spread else None))
    return result


def run_comparison(root: Path, *, validate: bool = True, runs: int = RANDOM_RUNS,
                   symbols: Optional[Sequence[str]] = None, log=lambda message: None) -> dict:
    symbols = list(symbols or load_universe(root / "config" / "backtest_universe.json"))
    db = StockDB.open(path=default_db_path(root))
    try:
        data = dict(train=load_histories(db, symbols, start=TRAIN[0], end=TRAIN[1]),
                    valid=load_histories(db, symbols, start=VALIDATION[0], end=VALIDATION[1]),
                    warm=load_histories(db, symbols, start=SWING_WARMUP_START, end=VALIDATION[1]))
    finally:
        db.close()
    return compare(symbols, data, validate=validate, runs=runs, log=log)


def compare(symbols: Sequence[str], data: dict, *, validate: bool = True, runs: int = RANDOM_RUNS,
            log=lambda message: None) -> dict:
    """data: train／valid／warm 各為 (histories, ex_rights)。warm 是含暖機期的驗證段，給波段原型用。"""
    (train, train_events), (valid, valid_events), (warm, warm_events) = (
        data["train"], data["valid"], data["warm"])
    smile = evaluate(
        "smile", SMILE_AXES,
        lambda p: _smile_backtester(p).run_histories(train, start=TRAIN[0], end=TRAIN[1],
                                                     ex_rights=train_events),
        lambda p: _smile_backtester(p).run_histories(valid, start=VALIDATION[0], end=VALIDATION[1],
                                                     ex_rights=valid_events),
        lambda p: smile_random_baseline(_smile_backtester(p), valid, ex_rights=valid_events,
                                        start=VALIDATION[0], end=VALIDATION[1], runs=runs),
        validate=validate, log=log)
    swing = evaluate(
        "swing", SWING_AXES,
        lambda p: _swing_backtester(p).run_histories(train, ex_rights=train_events),
        lambda p: _swing_backtester(p).run_histories(warm, trade_start=VALIDATION[0],
                                                     ex_rights=warm_events),
        lambda p: swing_random_baseline(_swing_backtester(p), warm, ex_rights=warm_events,
                                        trade_start=VALIDATION[0], runs=runs),
        validate=validate, log=log)
    with_bars = [s for s in symbols if train.get(s) or valid.get(s)]
    return dict(
        symbols=symbols, symbols_with_data=len(with_bars),
        symbols_without_data=[s for s in symbols if s not in with_bars],
        train=dict(start=TRAIN[0], end=TRAIN[1]), validation=dict(start=VALIDATION[0], end=VALIDATION[1]),
        initial_cash=INITIAL_CASH, execution=EXECUTION, common=_public(COMMON),
        smile_fixed=SMILE_FIXED, swing_defaults=_swing_defaults(), random_runs=runs,
        bootstrap_runs=BOOTSTRAP_RUNS, seed=DEFAULT_SEED, validated=validate,
        strategies=dict(smile=smile, swing=swing))


def _public(values: dict) -> dict:
    return {k: v for k, v in values.items() if not k.startswith("_")}


def _swing_defaults() -> dict:
    from dataclasses import asdict
    return asdict(SwingParams())


def main(argv: Optional[Sequence[str]] = None) -> int:
    from bot.cli_encoding import configure_console
    from bot.strategy_comparison_report import render
    configure_console()
    parser = argparse.ArgumentParser(description="微笑方案與波段原型的比較（#19）")
    parser.add_argument("--root", type=Path, default=None, help="專案根目錄（預設目前目錄）")
    parser.add_argument("--output", type=Path, help="Markdown 報告路徑")
    parser.add_argument("--json", type=Path, help="完整結果 JSON 路徑")
    parser.add_argument("--runs", type=int, default=RANDOM_RUNS, help="隨機進場重複次數")
    parser.add_argument("--from-json", type=Path,
                        help="不重跑回測，只用先前存下的結果 JSON 重新產生報告")
    parser.add_argument("--train-only", action="store_true",
                        help="只跑參數決定段（開發與除錯用；不會碰驗證段）")
    args = parser.parse_args(argv)
    root = args.root.resolve() if args.root else Path.cwd()
    if args.from_json:
        source = args.from_json if args.from_json.is_absolute() else root / args.from_json
        result = json.loads(source.read_text(encoding="utf-8"))
    else:
        result = run_comparison(root, validate=not args.train_only, runs=args.runs,
                                log=lambda message: print(message, file=sys.stderr, flush=True))
    def write(path: Optional[Path], text: str) -> None:
        if path:
            path = path if path.is_absolute() else root / path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))

    if not args.from_json:
        write(args.json, json.dumps(result, ensure_ascii=False, indent=2))  # 先存結果，再產生報告
    report = render(result)
    write(args.output, report)
    if not args.output:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

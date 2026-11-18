"""crash_ladder_strategy -- 大跌分批加碼策略的參數決定與驗證（只讀資料庫、只跑回測）。

規則見 bot.crash_ladder。固定：每階間距 5%、共 3 階、每批權益的 1/3、買名單全部標的等金額。
比較：首次加碼跌幅（10／15／20%）× 每批持有天數（30／60／90 日曆天）。選法、名單、資金、成交假設、
通過標準與 #19 相同（只用參數決定段選；驗證段只跑一次；和「同樣每批金額與持有天數、隨機日子買」比）。

限制：參數決定段只有 4 次大跌事件（2020-03、2021-05、2021-07～10、2022），每組參數的輪數很少；
研究（docs/reports/crash-rebound.md）已看過全部期間的事件，所以驗證段只算弱證據，要靠前瞻追蹤。

    python -m bot.crash_ladder_strategy --output docs/reports/crash-ladder.md --json work/crash-ladder/result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from bot.backtest_universe import load_universe
from bot.benchmark_index import index_max_drawdown_pct
from bot.config import Settings
from bot.crash_ladder import CrashLadderBacktester, CrashLadderParams, crash_entry_rate
from bot.random_entry import DEFAULT_SEED, strategy_random_baseline
from bot.smile_execution import SmileExecution
from bot.stock_db import StockDB, default_db_path
from bot.strategy_comparison import (BOOTSTRAP_RUNS, COMMON, EXECUTION, INITIAL_CASH, RANDOM_RUNS,
                                     SWING_WARMUP_START, TRAIN, VALIDATION, _public, evaluate, rejudge,
                                     validation_index_mdd)
from bot.swing_backtest import load_histories

CRASH_FIXED = dict(step_pct=5.0, tranches=3)
CRASH_AXES = ("first_drop_pct", (10.0, 15.0, 20.0)), ("max_hold_days", (30, 60, 90))

# 積極版（2026-11-18，使用者要求「衝一波回彈」）：持有天數沿用基本版選定的 90 天，改比較每批的資金分配。
# 選法事先寫死：參數決定段年化報酬最高，且最大回撤不超過同期等權指數、每輪期望值 > 0、
# 相鄰組合的期望值也都 > 0；沒有組合符合時退回 #19 的選法。
AGGR_FIXED = dict(step_pct=5.0, tranches=3, max_hold_days=90)
AGGR_AXES = ("first_drop_pct", (10.0, 15.0, 20.0)), ("weighting", ("equal", "front", "all_in"))


def _crash_backtester(params: dict, fixed: Optional[dict] = None) -> CrashLadderBacktester:
    merged = {**(CRASH_FIXED if fixed is None else fixed), **params}
    return CrashLadderBacktester(Settings(**COMMON), CrashLadderParams(**merged),
                                 execution=SmileExecution(**EXECUTION), initial_cash=INITIAL_CASH)


def _aggr_backtester(params: dict) -> CrashLadderBacktester:
    return _crash_backtester(params, AGGR_FIXED)


def choose_aggressive(train_index_mdd: Optional[float]):
    from bot.strategy_comparison import choose, neighbours

    def pick(combos):
        def ok(c):
            m = c["train"]
            return (m["round_count"] > 0 and m["expectancy_pct"] > 0 and train_index_mdd is not None
                    and m["max_drawdown_pct"] <= train_index_mdd)
        pool = [c for c in combos if ok(c)
                and all(n["train"]["expectancy_pct"] > 0 for n in neighbours(c["index"], combos))]
        if not pool:
            return choose(combos)
        best = max(pool, key=lambda c: (c["train"]["annualized_return_pct"], tuple(-x for x in c["index"])))
        return dict(index=best["index"], params=best["params"], neighbours_all_positive=True,
                    rule="annualized_return_within_index_drawdown", train_index_mdd=train_index_mdd)
    return pick


def compare_crash(data: dict, *, validate: bool = True, runs: int = RANDOM_RUNS, log=lambda m: None,
                  index_mdd: Optional[float] = None, variant: str = "base") -> dict:
    (train, train_events), (warm, warm_events) = data["train"], data["warm"]
    aggressive = variant == "aggressive"
    make = _aggr_backtester if aggressive else _crash_backtester
    key = "crash_aggr" if aggressive else "crash"
    chooser = (choose_aggressive(index_max_drawdown_pct(train, train_events, start=TRAIN[0], end=TRAIN[1]))
               if aggressive else None)

    def baseline(p):
        bt = make(p)
        return strategy_random_baseline(
            lambda entry: bt.run_histories(warm, trade_start=VALIDATION[0], ex_rights=warm_events,
                                           random_entry=entry),
            crash_entry_rate, runs=runs)

    crash = evaluate(
        key, AGGR_AXES if aggressive else CRASH_AXES,
        lambda p: make(p).run_histories(train, ex_rights=train_events),
        lambda p: make(p).run_histories(warm, trade_start=VALIDATION[0], ex_rights=warm_events),
        baseline, validate=validate, log=log, index_mdd=index_mdd, chooser=chooser)
    return dict(train=dict(start=TRAIN[0], end=TRAIN[1]),
                validation=dict(start=VALIDATION[0], end=VALIDATION[1]),
                initial_cash=INITIAL_CASH, execution=EXECUTION, common=_public(COMMON),
                crash_fixed=AGGR_FIXED if aggressive else CRASH_FIXED, variant=variant,
                random_runs=runs, bootstrap_runs=BOOTSTRAP_RUNS, seed=DEFAULT_SEED,
                validated=validate, strategies={key: crash},
                benchmark=dict(index_max_drawdown_pct=index_mdd))


def run(root: Path, *, validate: bool = True, runs: int = RANDOM_RUNS, log=lambda m: None,
        variant: str = "base") -> dict:
    symbols = list(load_universe(root / "config" / "backtest_universe.json"))
    db = StockDB.open(path=default_db_path(root))
    try:
        data = dict(train=load_histories(db, symbols, start=TRAIN[0], end=TRAIN[1]),
                    warm=load_histories(db, symbols, start=SWING_WARMUP_START, end=VALIDATION[1]))
    finally:
        db.close()
    warm, warm_events = data["warm"]
    index_mdd = index_max_drawdown_pct(warm, warm_events, start=VALIDATION[0], end=VALIDATION[1])
    result = compare_crash(data, validate=validate, runs=runs, log=log, index_mdd=index_mdd, variant=variant)
    result["symbols"] = symbols
    return result


def render(result: dict) -> str:
    from bot.strategy_comparison_report import _num, _strategy_section, notes_block
    aggressive = result.get("variant") == "aggressive"
    s = result["strategies"]["crash_aggr" if aggressive else "crash"]
    f = result["crash_fixed"]
    lines = ["# 大跌分批加碼" + ("（積極版）" if aggressive else "") + "：參數決定與驗證", "",
             f"參數決定段 {result['train']['start']}～{result['train']['end']}，驗證段 "
             f"{result['validation']['start']}～{result['validation']['end']}。名單、資金（{result['initial_cash']:,} 元）、"
             f"成交假設（{result['execution']}）、通過標準與 #19 相同（`docs/swing-strategy-design.md` 四之 5）。", "",
             "## 規則", "",
             "- 市場：60 檔等權指數（除權息還原）。指數從前高回落、每第一次跨過一階就在次日開盤買一批；"
             f"共 {f['tranches']} 階、每階間距 {f['step_pct']:g}%；指數創新高時階數歸零。",
             (f"- 每批：依「每批分配」決定（平均＝各 1/3；前重＝1/2、1/4、1/4；一次全押＝第一階 100%），"
              "買名單內全部標的、等金額（零股）。不挑股。" if aggressive
              else f"- 每批：權益的 1/{f['tranches']}，買名單內全部標的、等金額（零股）。不挑股。"),
             (f"- 出場：每批持有 {f['max_hold_days']} 日曆天（沿用基本版選定值），到期次日開盤賣出；沒有停損。"
              if aggressive else "- 出場：每批持有固定日曆天數，到期次日開盤賣出；沒有停損。"),
             ("- 比較：首次加碼跌幅與每批分配（各 3 個值）。選法（事先寫死）：參數決定段年化報酬最高，且最大回撤"
              "不超過同期等權指數、每輪期望值與相鄰組合的期望值都 > 0。" if aggressive
              else "- 比較：首次加碼跌幅與持有天數（各 3 個值）。")
             + "隨機進場基準：同樣每批金額與持有天數，改在隨機的日子買。",
             "", "> 研究階段（`crash-rebound.md`）看過整段期間的大跌事件，所以驗證段只算弱證據；"
             "參數決定段只有 4 次大跌，輪數很少。真正的檢驗是前瞻追蹤。", ""] + notes_block(result)
    index_mdd = (result.get("benchmark") or {}).get("index_max_drawdown_pct")
    if index_mdd is not None:
        lines += [f"同期（驗證段）60 檔等權指數的最大回撤：{_num(index_mdd, 1, '%')}。", ""]
    lines += _strategy_section("crash_aggr" if aggressive else "crash", s, result["validated"])
    if result["validated"]:
        failed = [c["name"] for c in s["verdict"]["core"] if not c["passed"]]
        lines += ["## 結論", "",
                  ("**核心條件全部通過。** 驗證段只算弱證據，仍要等前瞻追蹤。" if not failed
                   else f"**沒有證據。** 未通過：{'；'.join(failed)}。"), ""]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from bot.cli_encoding import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description="大跌分批加碼的參數決定與驗證（只讀資料庫）")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--runs", type=int, default=RANDOM_RUNS)
    parser.add_argument("--train-only", action="store_true", help="只跑參數決定段，不碰驗證段")
    parser.add_argument("--from-json", type=Path)
    parser.add_argument("--rejudge", action="store_true")
    parser.add_argument("--variant", choices=("base", "aggressive"), default="base",
                        help="aggressive：積極版（比較每批資金分配，選年化最高且回撤不超過指數者）")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.from_json:
        result = json.loads(args.from_json.read_text(encoding="utf-8"))
        if args.rejudge:
            rejudge(result, validation_index_mdd(root, result["symbols"]))
    else:
        result = run(root, validate=not args.train_only, runs=args.runs, variant=args.variant,
                     log=lambda m: print(m, file=sys.stderr, flush=True))
    text = render(result)
    for path, content in ((args.json, json.dumps(result, ensure_ascii=False, indent=1, default=str)),
                          (args.output, text)):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

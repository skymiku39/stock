"""selection_strategy -- 選股策略 C2 的參數決定與驗證（只讀資料庫、只跑回測）。

C2 來自進場時機研究（docs/reports/entry-timing.md）：40 個條件中，只有「相對強度前 40%
（60 日漲幅在名單內的排名）且最近一期已公告的月營收年增 > 0%」通過事先標準。研究顯示挑「哪一天」
進場沒有優勢，有差的是「買哪一檔」，所以 C2 是選股條件。

規則：SwingBacktester 的規則二（entry="rs_revenue"）。進場條件固定（rs_min 0.6、yoy_min 0，
直接沿用研究，不再調）；出場、部位、成交假設、名單、資金與通過標準都和 #19 相同。只比較出場：
持有期限（研究的優勢在 20～40 個交易日出現）與移動停利 ATR 倍數。

紀律：參數只用參數決定段（2020～2023）選，選法與 #19 相同；驗證段只跑一次。研究階段已看過
驗證段的單因子結果，所以驗證段對 C2 只算弱證據，真正的檢驗是前瞻追蹤（2026-11 起）。

    python -m bot.selection_strategy --output docs/reports/selection-strategy.md --json work/selection-strategy/result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from bot.backtest_universe import load_universe
from bot.benchmark_index import index_max_drawdown_pct
from bot.random_entry import DEFAULT_SEED, swing_random_baseline
from bot.stock_db import StockDB, default_db_path
from bot.strategy_comparison import (BOOTSTRAP_RUNS, COMMON, EXECUTION, INITIAL_CASH, RANDOM_RUNS,
                                     SWING_WARMUP_START, TRAIN, VALIDATION, _public, _swing_backtester,
                                     evaluate, rejudge, validation_index_mdd)
from bot.swing_backtest import SwingParams, load_histories, load_revenue

C2_FIXED = dict(entry="rs_revenue", rs_min=0.6, yoy_min=0.0)
C2_AXES = ("max_hold_days", (30, 60, 90)), ("trail_atr", (2.0, 3.0, 4.0))


def _c2_backtester(params: dict):
    return _swing_backtester({**C2_FIXED, **params})


def compare_c2(data: dict, revenue: dict, *, validate: bool = True, runs: int = RANDOM_RUNS,
               log=lambda message: None, index_mdd: Optional[float] = None) -> dict:
    """data: train／warm 各為 (histories, ex_rights)；warm 是含暖機期的驗證段。"""
    (train, train_events), (warm, warm_events) = data["train"], data["warm"]
    c2 = evaluate(
        "c2", C2_AXES,
        lambda p: _c2_backtester(p).run_histories(train, ex_rights=train_events, revenue=revenue),
        lambda p: _c2_backtester(p).run_histories(warm, trade_start=VALIDATION[0],
                                                  ex_rights=warm_events, revenue=revenue),
        lambda p: swing_random_baseline(_c2_backtester(p), warm, ex_rights=warm_events,
                                        trade_start=VALIDATION[0], runs=runs, revenue=revenue),
        validate=validate, log=log, index_mdd=index_mdd)
    defaults = {k: v for k, v in vars(SwingParams(**C2_FIXED)).items()
                if k not in ("breakout_days", "fast_ma", "slow_ma")}
    return dict(train=dict(start=TRAIN[0], end=TRAIN[1]),
                validation=dict(start=VALIDATION[0], end=VALIDATION[1]),
                initial_cash=INITIAL_CASH, execution=EXECUTION, common=_public(COMMON),
                c2_defaults=defaults, random_runs=runs, bootstrap_runs=BOOTSTRAP_RUNS,
                seed=DEFAULT_SEED, validated=validate, strategies=dict(c2=c2),
                benchmark=dict(index_max_drawdown_pct=index_mdd))


def run(root: Path, *, validate: bool = True, runs: int = RANDOM_RUNS,
        log=lambda message: None) -> dict:
    symbols = list(load_universe(root / "config" / "backtest_universe.json"))
    db = StockDB.open(path=default_db_path(root))
    try:
        data = dict(train=load_histories(db, symbols, start=TRAIN[0], end=TRAIN[1]),
                    warm=load_histories(db, symbols, start=SWING_WARMUP_START, end=VALIDATION[1]))
        revenue = load_revenue(db, symbols)
    finally:
        db.close()
    warm, warm_events = data["warm"]
    index_mdd = index_max_drawdown_pct(warm, warm_events, start=VALIDATION[0], end=VALIDATION[1])
    result = compare_c2(data, revenue, validate=validate, runs=runs, log=log, index_mdd=index_mdd)
    result.update(symbols=symbols, revenue_symbols=sum(1 for rows in revenue.values() if rows))
    return result


def render(result: dict) -> str:
    from bot.strategy_comparison_report import _num, _strategy_section, notes_block
    s = result["strategies"]["c2"]
    d = result["c2_defaults"]
    lines = ["# 選股策略 C2：參數決定與驗證", "",
             f"參數決定段 {result['train']['start']}～{result['train']['end']}，驗證段 "
             f"{result['validation']['start']}～{result['validation']['end']}。名單、資金（{result['initial_cash']:,} 元）、"
             f"成交假設（{result['execution']}）、通過標準與 #19 相同（`docs/swing-strategy-design.md` 四之 5）。", "",
             "## 規則", "",
             f"- 進場（固定，沿用 `docs/reports/entry-timing.md` 的 C2）：收盤時 {d['momentum_days']} 日漲幅在名單內的排名百分位 "
             f"≥ {d['rs_min']:g}，且最近一期已公告的月營收年增率 > {d['yoy_min']:g}%（次月 11 日起視為已知）；次日開盤買進。"
             "候選多於空位時取漲幅較大者。",
             f"- 出場與部位（同波段原型）：初始停損 {d['initial_stop_atr']:g} 倍 ATR（{d['atr_days']} 日）、移動停利、持有期限；"
             f"每筆承擔權益 {d['risk_pct']:g}% 的風險、單檔上限 {d['max_position_pct']:.0f}%、最多 {d['max_positions']} 檔。",
             "- 比較：持有期限與移動停利 ATR 倍數（各 3 個值）。", "",
             "> 驗證段的單因子結果在研究階段已看過，所以驗證段對 C2 只算弱證據；真正的檢驗是前瞻追蹤（2026-11 起）。",
             ""] + notes_block(result)
    index_mdd = (result.get("benchmark") or {}).get("index_max_drawdown_pct")
    if index_mdd is not None:
        lines += [f"同期（驗證段）60 檔等權指數的最大回撤：{_num(index_mdd, 1, '%')}。", ""]
    lines += _strategy_section("c2", s, result["validated"])
    if result["validated"]:
        failed = [c["name"] for c in s["verdict"]["core"] if not c["passed"]]
        lines += ["## 結論", "",
                  ("**核心條件全部通過。** 因為驗證段只算弱證據，仍要等前瞻追蹤累積到參考輪數才算有證據。"
                   if not failed else f"**沒有證據。** 未通過：{'；'.join(failed)}。"), ""]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from bot.cli_encoding import configure_console
    configure_console()
    parser = argparse.ArgumentParser(description="選股策略 C2 的參數決定與驗證（只讀資料庫）")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, help="Markdown 報告路徑")
    parser.add_argument("--json", type=Path, help="完整結果 JSON 路徑")
    parser.add_argument("--runs", type=int, default=RANDOM_RUNS, help="隨機進場重複次數")
    parser.add_argument("--train-only", action="store_true", help="只跑參數決定段，不碰驗證段")
    parser.add_argument("--from-json", type=Path, help="不重跑回測，讀先前存下的結果")
    parser.add_argument("--rejudge", action="store_true",
                        help="搭配 --from-json：依目前的通過標準重新判定（讀資料庫算同期指數回撤）")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.from_json:
        result = json.loads(args.from_json.read_text(encoding="utf-8"))
        if args.rejudge:
            rejudge(result, validation_index_mdd(root, result["symbols"]))
    else:
        result = run(root, validate=not args.train_only, runs=args.runs,
                     log=lambda message: print(message, file=sys.stderr, flush=True))
    text = render(result)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

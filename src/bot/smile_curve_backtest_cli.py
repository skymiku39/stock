"""stock-smile-backtest -- 微笑曲線策略日 K 回測。

用法
====
    uv run stock-smile-backtest --symbols 2330,0050 --start 2020-01-01
    uv run stock-smile-backtest --symbols 2330 --days 365 --fetch
    uv run stock-smile-backtest --symbols 2330 --tiers "1:1,3:2,5:3,10:5"
    uv run stock-smile-backtest --symbols 2317,2603 --start 2020-01-02 --random-baseline --seed 20261116
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path
from typing import List, Optional

from bot.cli_encoding import configure_console
from bot.config import Settings
from bot.random_entry import DEFAULT_RUNS, DEFAULT_SEED, smile_random_baseline
from bot.round_metrics import account_metrics, summarize_rounds
from bot.smile_execution import SmileExecution
from bot.smile_curve_backtest import SmileBacktestSummary, SmileCurveBacktester
from bot.stock_db import StockDB, default_db_path
from bot.utils import get_logger, mk_folder, now_tw


def _normalize_symbol(raw: str) -> str:
    """PowerShell 可能把 0050 解析成 50，補回台股代號格式。"""
    s = raw.strip()
    if s.isdigit() and len(s) < 4:
        return s.zfill(4)
    return s


def _resolve_dates(days: int, start: Optional[str], end: Optional[str]) -> tuple[str, str]:
    today = now_tw().date()
    if start:
        start_d = dt.date.fromisoformat(start)
        end_d = dt.date.fromisoformat(end) if end else today
        if start_d > end_d:
            raise ValueError("start 不可晚於 end")
        return start_d.isoformat(), end_d.isoformat()
    if days <= 0:
        days = 365
    start_d = today - dt.timedelta(days=int(days * 1.45))
    return start_d.isoformat(), today.isoformat()


def _ensure_daily_data(
    symbols: List[str],
    start: str,
    end: str,
    *,
    root: Path,
    db: StockDB,
    logger,
) -> None:
    from bot.technicals import fetch_kline_range

    start_d = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)
    total = len(symbols)
    for i, sym in enumerate(symbols, start=1):
        bars = db.get_price_history(sym, limit=1, ascending=True)
        earliest = bars[0].date[:10] if bars else None
        latest = db.latest_price_date(sym)
        if earliest and earliest <= start and latest and latest >= end:
            logger.info("[%d/%d] %s 日K已覆蓋 %s ~ %s", i, total, sym, start, end)
            continue
        logger.info("[%d/%d] 補日K %s (%s ~ %s)", i, total, sym, start, end)
        fetch_kline_range(
            sym,
            start_date=start_d,
            end_date=end_d,
            root=root,
            logger=logger,
            save_to_db=True,
        )


def _print_summary(summary: SmileBacktestSummary) -> None:
    print("\n=== 微笑曲線回測摘要 ===")
    print(summary.settings_note)
    if summary.start:
        print(f"區間: {summary.start} ~ {summary.end}")
    print(f"回合數: {len(summary.all_round_trips)}")
    print(f"已實現: {summary.total_pnl_twd:,.0f} 元 | 未實現(含出清成本): {summary.unrealized_pnl_twd:,.0f} 元")
    print(f"總損益: {summary.net_pnl_twd:,.0f} 元 | 本金報酬: {summary.return_pct:.2f}%")
    print(f"最大日終回撤: {summary.max_drawdown_pct:.2f}% / {summary.max_drawdown_twd:,.0f} 元")
    if summary.halted_date:
        print(f"浮虧護欄觸發: {summary.halted_date} 起停止買進")
    print(f"已平倉勝率: {summary.win_rate:.1f}%（只賣獲利單，不代表總報酬）")
    m = summarize_rounds(summary.rounds)
    a = account_metrics(summary.equity_curve, summary.initial_cash, summary.buy_cost_total_twd)
    pf = "n/a" if m["profit_factor"] is None else f"{m['profit_factor']:.2f}"
    rm = "n/a" if a["return_over_mdd"] is None else f"{a['return_over_mdd']:.2f}"
    to = "n/a" if a["annual_turnover"] is None else f"{a['annual_turnover']:.2f}"
    opened = sum(1 for r in summary.results if r.open_lots > 0)
    print("--- 回合指標（整倉出清為一回合；未平倉不計）---")
    print(f"回合數: {m['round_count']} | 勝率: {m['win_rate']:.1f}% | 獲利因子: {pf}")
    print(f"期望值: {m['expectancy_twd']:,.0f} 元 / {m['expectancy_pct']:.2f}% 每回合")
    print(f"持有天數 平均/中位/最長: {m['hold_days_mean']:.1f} / {m['hold_days_median']:.1f} / "
          f"{m['hold_days_max']} | >90天: {m['over_90_days_pct']:.1f}%")
    print(f"出場原因: {m['exit_reason_counts']}")
    print(f"年化報酬: {a['annualized_return_pct']:.2f}% | 報酬/MDD: {rm} | "
          f"平均資金使用率: {a['avg_capital_utilization_pct']:.1f}% | 年換手率: {to}")
    print(f"未平倉檔數: {opened} | 未實現: {summary.unrealized_pnl_twd:,.0f} 元")
    applied = [d for d in summary.distributions if d["status"] == "applied"]
    skipped = [d for d in summary.distributions if d["status"] != "applied"]
    print(f"除權息調整: {len(applied)} 次 | 持倉配發入帳: {summary.distribution_total_twd:,.0f} 元"
          f"（已含在損益內）| 資料不足暫停訊號: {len(skipped)} 次")
    if summary.ex_rights_no_events:
        print(f"區間內查無除權息事件（未調整，可能是資料未收錄）: {', '.join(summary.ex_rights_no_events)}")
    for d in summary.data_ended:
        tail = (f"{d['forced_exit_date']} 以 {d['exit_price']:.2f} 強制平倉 {d['lot_count']} 批，"
                f"損益 {d['pnl_twd']:,.0f} 元" if d["forced_exit_date"] else "當時無持倉")
        print(f"資料中斷: {d['symbol']} 最後一根日 K {d['last_date']}（之後還有 "
              f"{d['sessions_after']} 個交易日）；{tail}")
    print()
    for r in summary.results:
        if r.skip_reason:
            print(f"  {r.symbol}: 略過 ({r.skip_reason})")
            continue
        print(
            f"  {r.symbol}: {r.trade_count} 筆 | "
            f"已實現 {r.total_pnl_twd:,.0f} | 未實現 {r.unrealized_pnl_twd:,.0f} | "
            f"週期 {r.cycle_count} | 未平倉 lot {r.open_lots}",
        )


def _export_csv(summary: SmileBacktestSummary, path: Path) -> None:
    mk_folder(path.parent)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "symbol", "entry_date", "exit_date", "entry_price", "exit_price",
            "quantity", "unit", "entry_reason", "exit_reason",
            "pnl_twd", "pnl_pct", "reference", "distribution_twd",
        ])
        for t in summary.all_round_trips:
            w.writerow([
                t.symbol, t.entry_date, t.exit_date,
                f"{t.entry_price:.4f}", f"{t.exit_price:.4f}",
                t.quantity, t.unit, t.entry_reason, t.exit_reason,
                f"{t.pnl_twd:.2f}", f"{t.pnl_pct:.4f}", f"{t.reference:.4f}",
                f"{t.distribution_twd:.2f}",
            ])


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="微笑曲線策略日 K 回測")
    parser.add_argument("--symbols", required=True, help="逗號分隔股票代號")
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="結束日期 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=365, help="未指定 start 時往回 N 日")
    parser.add_argument("--tiers", help="覆寫 SMILE_BUY_TIERS，例 1:1,3:2,5:3,8:4")
    parser.add_argument("--base-lot", type=int, help="每階基礎張數")
    parser.add_argument("--reference", help="固定標準價 SYMBOL:PRICE，例 2330:580")
    parser.add_argument("--max-fund", type=int, help="資金上限")
    parser.add_argument("--slippage-bps", type=float, default=10, help="每邊滑價 bps，10 = 0.1%%")
    parser.add_argument("--participation", type=float, default=.001, help="前一日成交股數參與率，.001 = 0.1%%")
    parser.add_argument("--fetch", action="store_true", help="缺日K時自動抓取")
    parser.add_argument("--no-ex-rights", action="store_true",
                        help="不套用除權息調整（對照用；預設會套用 ex_rights_events）")
    parser.add_argument("--random-baseline", action="store_true",
                        help="輸出隨機進場基準（JSON）：同樣的出場與部位規則，進場日隨機，重複多次取分布")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="隨機進場重複次數")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="隨機進場亂數種子")
    parser.add_argument("--output", help="匯出 CSV 路徑")
    parser.add_argument("--json", action="store_true", help="輸出 JSON 摘要")
    parser.add_argument(
        "--root", type=Path, default=None,
        help="專案資料／設定根目錄（未指定時使用目前工作目錄）",
    )
    args = parser.parse_args(argv)

    logger = get_logger("smile-backtest")
    root = args.root.resolve() if args.root else Path.cwd()
    symbols = [_normalize_symbol(s) for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        logger.error("請指定 --symbols")
        return 1

    start, end = _resolve_dates(args.days, args.start, args.end)
    overrides: dict = {"symbols": symbols, "_env_file": str(root / ".env")}
    if args.tiers:
        overrides["smile_buy_tiers"] = args.tiers
    if args.base_lot is not None:
        overrides["smile_base_lot"] = args.base_lot
    if args.max_fund is not None:
        overrides["max_fund"] = args.max_fund
        overrides["daily_fund_budget"] = 0
    if args.reference:
        parts = args.reference.split(":")
        if len(parts) == 2:
            sym = _normalize_symbol(parts[0])
            overrides["smile_reference_prices"] = {sym: float(parts[1])}

    settings = Settings(**overrides)  # type: ignore[call-arg]
    db = StockDB.open(path=default_db_path(root))
    try:
        if args.fetch:
            _ensure_daily_data(symbols, start, end, root=root, db=db, logger=logger)

        execution = SmileExecution(slippage_bps=args.slippage_bps, participation=args.participation)
        backtester = SmileCurveBacktester(settings, execution=execution,
                                          apply_ex_rights=not args.no_ex_rights)
        if args.random_baseline:
            histories, events = backtester.load(symbols, db, start=start, end=end)
            payload = smile_random_baseline(backtester, histories, ex_rights=events, start=start,
                                            end=end, runs=args.runs, seed=args.seed)
            payload.update(symbols=sorted(histories), start=start, end=end)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        summary = backtester.run_many(
            symbols, root=root, start=start, end=end, db=db,
        )
        summary.start = start
        summary.end = end

        if args.json:
            payload = summary.to_dict()
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_summary(summary)

        if args.output:
            out = Path(args.output)
            if not out.is_absolute():
                out = root / out
            _export_csv(summary, out)
            logger.info("已匯出 %s", out)

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

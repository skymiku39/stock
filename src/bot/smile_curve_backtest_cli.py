"""stock-smile-backtest -- 微笑曲線策略日 K 回測。

用法
====
    uv run stock-smile-backtest --symbols 2330,0050 --start 2020-01-01
    uv run stock-smile-backtest --symbols 2330 --days 365 --fetch
    uv run stock-smile-backtest --symbols 2330 --tiers "1:1,3:2,5:3,10:5"
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

from bot.config import Settings
from bot.smile_curve_backtest import SmileBacktestSummary, SmileCurveBacktester
from bot.stock_db import StockDB, default_db_path
from bot.utils import get_logger, mk_folder, now_tw


def _normalize_symbol(raw: str) -> str:
    """PowerShell 可能把 0050 解析成 50，補回台股代號格式。"""
    s = raw.strip()
    if s.isdigit() and len(s) < 4:
        return s.zfill(4)
    return s


def _resolve_dates(days: int, start: str | None, end: str | None) -> tuple[str, str]:
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
    symbols: list[str],
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
    print(f"總淨利: {summary.total_pnl_twd:,.0f} 元")
    print(f"勝率: {summary.win_rate:.1f}%")
    print()
    for r in summary.results:
        if r.skip_reason:
            print(f"  {r.symbol}: 略過 ({r.skip_reason})")
            continue
        print(
            f"  {r.symbol}: {r.trade_count} 筆 | "
            f"淨利 {r.total_pnl_twd:,.0f} | 勝率 {r.win_rate:.1f}% | "
            f"週期 {r.cycle_count} | 未平倉 lot {r.open_lots}",
        )


def _export_csv(summary: SmileBacktestSummary, path: Path) -> None:
    mk_folder(path.parent)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "symbol", "entry_date", "exit_date", "entry_price", "exit_price",
            "quantity", "unit", "entry_reason", "exit_reason",
            "pnl_twd", "pnl_pct", "reference",
        ])
        for t in summary.all_round_trips:
            w.writerow([
                t.symbol, t.entry_date, t.exit_date,
                f"{t.entry_price:.4f}", f"{t.exit_price:.4f}",
                t.quantity, t.unit, t.entry_reason, t.exit_reason,
                f"{t.pnl_twd:.2f}", f"{t.pnl_pct:.4f}", f"{t.reference:.4f}",
            ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="微笑曲線策略日 K 回測")
    parser.add_argument("--symbols", required=True, help="逗號分隔股票代號")
    parser.add_argument("--start", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="結束日期 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=365, help="未指定 start 時往回 N 日")
    parser.add_argument("--tiers", help="覆寫 SMILE_BUY_TIERS，例 1:1,3:2,5:3,8:4")
    parser.add_argument("--base-lot", type=int, help="每階基礎張數")
    parser.add_argument("--reference", help="固定標準價 SYMBOL:PRICE，例 2330:580")
    parser.add_argument("--max-fund", type=int, help="資金上限")
    parser.add_argument("--fetch", action="store_true", help="缺日K時自動抓取")
    parser.add_argument("--output", help="匯出 CSV 路徑")
    parser.add_argument("--json", action="store_true", help="輸出 JSON 摘要")
    args = parser.parse_args(argv)

    logger = get_logger("smile-backtest")
    root = Path.cwd()
    symbols = [_normalize_symbol(s) for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        logger.error("請指定 --symbols")
        return 1

    start, end = _resolve_dates(args.days, args.start, args.end)
    overrides: dict = {"symbols": symbols, "_env_file": None}
    if args.tiers:
        overrides["smile_buy_tiers"] = args.tiers
    if args.base_lot is not None:
        overrides["smile_base_lot"] = args.base_lot
    if args.max_fund is not None:
        overrides["max_fund"] = args.max_fund
    if args.reference:
        parts = args.reference.split(":")
        if len(parts) == 2:
            sym = _normalize_symbol(parts[0])
            overrides["smile_reference_prices"] = {sym: float(parts[1])}

    settings = Settings(**overrides)  # type: ignore[call-arg]
    db = StockDB.open(path=default_db_path(root))

    if args.fetch:
        _ensure_daily_data(symbols, start, end, root=root, db=db, logger=logger)

    summary = SmileCurveBacktester(settings).run_many(
        symbols, root=root, start=start, end=end, db=db,
    )
    summary.start = start
    summary.end = end

    if args.json:
        payload = {
            "settings_note": summary.settings_note,
            "start": start,
            "end": end,
            "total_pnl_twd": summary.total_pnl_twd,
            "win_rate": summary.win_rate,
            "trade_count": len(summary.all_round_trips),
            "symbols": [
                {
                    "symbol": r.symbol,
                    "trade_count": r.trade_count,
                    "total_pnl_twd": r.total_pnl_twd,
                    "win_rate": r.win_rate,
                    "cycle_count": r.cycle_count,
                    "open_lots": r.open_lots,
                    "skip_reason": r.skip_reason,
                }
                for r in summary.results
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_summary(summary)

    if args.output:
        out = Path(args.output)
        _export_csv(summary, out)
        logger.info("已匯出 %s", out)

    return 0


if __name__ == "__main__":
    sys.exit(main())

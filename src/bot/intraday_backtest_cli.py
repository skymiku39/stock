"""stock-intraday-backtest -- LLM 清單量化回測（日 K 長區間 / 分 K 短區間）。

用法
====
    uv run stock-intraday-backtest --days 3
    uv run stock-intraday-backtest --start 2020-01-01 --granularity daily --fetch
    uv run stock-intraday-backtest --symbols 6446,2368 --days 5 --granularity 1m --fetch
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

from bot.config import Settings
from bot.daily_backtest import DailyBacktester
from bot.intraday_backtest import BacktestSummary, IntradayBacktester
from bot.intraday_history import fetch_and_store_intraday, iter_trading_days
from bot.llm_symbol_picks import collect_llm_symbols, llm_symbol_metadata
from bot.stock_db import StockDB, default_db_path
from bot.utils import get_logger, mk_folder, now_tw


def _resolve_dates(days: int, start: str | None, end: str | None) -> tuple[str, str]:
    today = now_tw().date()
    if start:
        start_d = dt.date.fromisoformat(start)
        end_d = dt.date.fromisoformat(end) if end else today
        if start_d > end_d:
            raise ValueError("start 不可晚於 end")
        return start_d.isoformat(), end_d.isoformat()
    if days <= 0:
        days = 3
    trading = iter_trading_days(
        today - dt.timedelta(days=max(days * 2, days + 14)),
        today,
    )
    if not trading:
        d = today.isoformat()
        return d, d
    picked = trading[-days:]
    return picked[0].isoformat(), picked[-1].isoformat()


def _pick_granularity(
    arg: str,
    start: str,
    end: str,
) -> str:
    if arg in ("daily", "1m"):
        return arg
    span = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
    return "daily" if span > 90 else "1m"


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
            logger.debug("%s 日K已覆蓋 %s ~ %s", sym, start, end)
            continue
        before = len(db.get_price_history(sym, ascending=True))
        logger.info("[%d/%d] 補日K %s (%s ~ %s)", i, total, sym, start, end)
        try:
            fetch_kline_range(
                sym,
                start_date=start_d,
                end_date=end_d,
                root=root,
                save_to_db=True,
                request_delay_sec=1.2,
                skip_existing_months=True,
            )
        except Exception:
            logger.exception("補日K失敗: %s", sym)
        after = len(db.get_price_history(sym, ascending=True))
        if after <= before:
            logger.warning(
                "%s 日K無新增 (可能 TWSE 限流 403)；將以現有 %d 根回測",
                sym, after,
            )


def _ensure_intraday_data(
    symbols: list[str],
    start: str,
    end: str,
    *,
    settings: Settings,
    root: Path,
    db: StockDB,
    logger,
) -> None:
    from bot.broker import SjBroker

    need_fetch: list[str] = []
    start_d = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)
    for sym in symbols:
        for day in iter_trading_days(start_d, end_d):
            n = db.count_intraday_bars(
                sym, interval="1m", start=day.isoformat(), end=day.isoformat(),
            )
            if n < 200:
                need_fetch.append(sym)
                break

    if not need_fetch:
        logger.info("分 K 資料已齊，跳過 Shioaji 補抓")
        return

    if not settings.api_key or not settings.secret_key:
        logger.warning(
            "缺少 API_KEY，無法補抓分 K；僅對已有資料回測 (%d 檔可能缺資料)",
            len(need_fetch),
        )
        return

    broker = SjBroker(settings, logger=logger)
    if not broker.login():
        logger.error("Shioaji 登入失敗，無法補抓分 K")
        return
    try:
        for sym in need_fetch:
            logger.info("補抓 %s 分 K %s ~ %s", sym, start, end)
            fetch_and_store_intraday(
                broker,
                sym,
                start_d,
                end_d,
                interval="1m",
                chunk_days=1,
                skip_existing_days=True,
                save_csv=True,
                root=root,
                db=db,
                request_delay_sec=0.4,
                logger=logger,
            )
    finally:
        broker.logout()


def _export_csv(summary: BacktestSummary, path: Path) -> None:
    mk_folder(str(path.parent))
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "symbol", "trade_date", "entry_ts", "exit_ts",
            "entry_price", "exit_price", "qty", "unit",
            "entry_reason", "exit_reason", "pnl_twd", "pnl_pct", "entry_pct_chg",
        ])
        for r in summary.results:
            for t in r.trades:
                w.writerow([
                    t.symbol, t.trade_date, t.entry_ts, t.exit_ts,
                    f"{t.entry_price:.4f}", f"{t.exit_price:.4f}",
                    t.quantity, t.unit,
                    t.entry_reason, t.exit_reason,
                    f"{t.pnl_twd:.2f}", f"{t.pnl_pct:.4f}", f"{t.entry_pct_chg:.4f}",
                ])


def _print_summary(summary: BacktestSummary, meta: dict) -> None:
    trades = summary.all_trades
    print("\n=== 量化回測摘要 ===")
    print(summary.settings_note)
    print(f"交易筆數: {len(trades)} | 勝率: {summary.win_rate:.1f}% | 總損益: {summary.total_pnl_twd:,.0f} 元")
    print(f"{'代號':<8} {'LLM':<6} {'分數':<8} {'天數':<5} {'筆數':<5} {'勝':<4} {'損益(元)':<12} 備註")
    print("-" * 72)
    for r in summary.results:
        m = meta.get(r.symbol, {})
        rank = m.get("rank", "-")
        score = m.get("day_trade_score", "-")
        if score != "-" and score is not None:
            score = f"{float(score):.1f}"
        note = r.skip_reason or ""
        print(
            f"{r.symbol:<8} {rank!s:<6} {score!s:<8} {r.bar_days:<5} "
            f"{len(r.trades):<5} {r.win_count:<4} {r.total_pnl_twd:>10,.0f}  {note}"
        )
    if trades:
        print("\n--- 最近成交明細 ---")
        for t in trades[-10:]:
            print(
                f"{t.trade_date} {t.symbol} {t.entry_ts[11:16]}→{t.exit_ts[11:16]} "
                f"{t.exit_reason} pnl={t.pnl_twd:,.0f} ({t.pnl_pct:+.2f}%)"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM 清單分 K 量化回測")
    parser.add_argument("--symbols", help="覆寫股票清單 (逗號分隔)；預設取近期 LLM 報告")
    parser.add_argument(
        "--days", type=int, default=0,
        help="回測最近 N 個交易日 (與 --start 擇一；預設 3)",
    )
    parser.add_argument("--start", help="開始日期 YYYY-MM-DD，例 2020-01-01")
    parser.add_argument("--end", help="結束日期 YYYY-MM-DD (預設今天)")
    parser.add_argument(
        "--granularity",
        choices=("auto", "daily", "1m"),
        default="auto",
        help="auto=長區間用日K、短區間用分K",
    )
    parser.add_argument(
        "--fetch", action="store_true",
        help="自動補資料 (日K→TWSE；分K→Shioaji)",
    )
    parser.add_argument("--lookback", type=int, default=5, help="LLM 報告往回找幾天")
    parser.add_argument(
        "--csv", default="",
        help="匯出交易明細 CSV (預設 data/reports/backtest_YYYY-MM-DD.csv)",
    )
    parser.add_argument("--json", default="", help="另存 JSON 摘要")
    args = parser.parse_args(argv)

    logger = get_logger("intraday-backtest")
    root = Path.cwd()
    settings = Settings()
    try:
        start, end = _resolve_dates(args.days, args.start, args.end)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    granularity = _pick_granularity(args.granularity, start, end)

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = collect_llm_symbols(root, lookback_days=args.lookback)
    if not symbols:
        logger.error("找不到 LLM 股票清單；請用 --symbols 指定")
        return 1

    logger.info("回測清單 (%d 檔): %s", len(symbols), ",".join(symbols))
    logger.info("區間: %s ~ %s | 粒度: %s", start, end, granularity)

    db = StockDB.open(path=default_db_path(root))
    if args.fetch:
        if granularity == "daily":
            _ensure_daily_data(symbols, start, end, root=root, db=db, logger=logger)
        else:
            _ensure_intraday_data(
                symbols, start, end, settings=settings, root=root, db=db, logger=logger,
            )

    meta = llm_symbol_metadata(root, symbols)
    if granularity == "daily":
        engine = DailyBacktester(settings)
    else:
        engine = IntradayBacktester(settings)
    summary = engine.run_many(symbols, root=root, start=start, end=end, db=db)

    tag = f"{start}_{end}_{granularity}"
    csv_path = Path(
        args.csv or f"data/reports/backtest_{tag}.csv",
    )
    _export_csv(summary, csv_path)
    logger.info("交易明細: %s", csv_path)

    if args.json:
        payload = {
            "start": start,
            "end": end,
            "granularity": granularity,
            "symbols": symbols,
            "settings_note": summary.settings_note,
            "total_pnl_twd": summary.total_pnl_twd,
            "win_rate": summary.win_rate,
            "trade_count": len(summary.all_trades),
            "by_symbol": [
                {
                    "symbol": r.symbol,
                    "trades": len(r.trades),
                    "wins": r.win_count,
                    "pnl_twd": r.total_pnl_twd,
                    "bar_days": r.bar_days,
                    "skip_reason": r.skip_reason,
                    "llm_meta": meta.get(r.symbol, {}),
                }
                for r in summary.results
            ],
        }
        jp = Path(args.json)
        mk_folder(str(jp.parent))
        jp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_summary(summary, meta)
    return 0


if __name__ == "__main__":
    sys.exit(main())

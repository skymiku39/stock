"""stock-smile-screen -- 微笑曲線複合選股 CLI。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bot.app_bootstrap import get_or_create_bus
from bot.config import Settings
from bot.smile_curve_backtest_cli import (
    _ensure_daily_data,
    _normalize_symbol,
    _resolve_dates,
)
from bot.smile_curve_screener import (
    SmileScreenReport,
    collect_universe_tickers,
    screen_smile_candidates,
)
from bot.stock_db import StockDB, default_db_path
from bot.utils import get_logger


def _print_report(report: SmileScreenReport) -> None:
    print("\n=== 微笑曲線複合選股 ===")
    print(f"區間: {report.start} ~ {report.end}")
    print(
        f"總資金 {report.total_fund:,.0f} 元 | "
        f"每檔 {report.fund_per_symbol:,.0f} 元 | Top {report.top_n}",
    )
    print(f"共識池 {report.universe_size} 檔 → 進入分析 {report.filtered_size} 檔")
    for note in report.notes:
        print(f"• {note}")
    if not report.candidates:
        return

    print("\n--- 建議組合 ---")
    for c in report.selected:
        add = " [共識加碼]" if c.has_consensus_add else ""
        print(
            f"  #{c.rank} {c.symbol} {c.name}{add} | "
            f"分數 {c.composite_score:.1f} | {c.smile_fit} | "
            f"ETF×{c.etf_count} | 價 {c.price:.1f} | "
            f"波動 {c.ann_vol_pct:.1f}% | 回測 {c.trade_count} 回合 | "
            f"淨利 {c.backtest_pnl_twd:,.0f}",
        )

    print("\n--- 候選排行 (前 15) ---")
    for c in report.candidates[:15]:
        print(
            f"  {c.rank:2d}. {c.symbol} score={c.composite_score:5.1f} "
            f"etf={c.etf_count} vol={c.ann_vol_pct:4.1f}% "
            f"trades={c.trade_count:3d} fit={c.smile_fit}",
        )

    sug = report.settings_suggestion
    print("\n--- 建議 .env 片段 ---")
    print(f"USE_ODD_LOT={sug['USE_ODD_LOT']}")
    print(f"SMILE_BUY_TIERS={sug['SMILE_BUY_TIERS']}")
    print(f"MAX_FUND={sug['MAX_FUND']}")
    print(f"MAX_OPEN_POSITIONS={sug['MAX_OPEN_POSITIONS']}")
    print(f"MANUAL_HOLD_SYMBOLS={sug['MANUAL_HOLD_SYMBOLS']}")
    print(f"SELL_PROFIT_TARGETS={sug['SELL_PROFIT_TARGETS']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="微笑曲線複合選股：ETF 共識 + 波動回檔 + 資金可負擔 + 回測驗證",
    )
    parser.add_argument("--total-fund", type=int, default=300_000, help="總資金 (元)")
    parser.add_argument("--top-n", type=int, default=5, help="建議持有檔數")
    parser.add_argument("--min-etf", type=int, default=2, help="最少 ETF 共識檔數")
    parser.add_argument("--max-price", type=float, default=300.0, help="最高股價 (0=不限)")
    parser.add_argument("--min-vol", type=float, default=35.0, help="最低年化波動 %% (0=不限)")
    parser.add_argument("--universe-limit", type=int, default=30, help="共識池最多分析檔數")
    parser.add_argument("--symbols", help="額外/覆寫候選池 (逗號分隔)；無 ETF 快照時必填")
    parser.add_argument("--start", help="回測起始 YYYY-MM-DD")
    parser.add_argument("--end", help="回測結束 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=900, help="未指定 start 時往回推算")
    parser.add_argument("--fetch", action="store_true", help="缺日 K 時自動抓取")
    parser.add_argument("--odd-lot", action="store_true", help="強制零股回測")
    parser.add_argument("--no-odd-lot", action="store_true", help="強制整股回測")
    parser.add_argument("--tiers", help="覆寫 SMILE_BUY_TIERS")
    parser.add_argument("--output", help="匯出 JSON 報告路徑")
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    args = parser.parse_args(argv)

    logger = get_logger("smile-screen")
    root = Path.cwd()
    start, end = _resolve_dates(args.days, args.start, args.end)

    extra: list[str] | None = None
    if args.symbols:
        extra = [_normalize_symbol(s) for s in args.symbols.split(",") if s.strip()]

    use_odd_lot: bool | None = None
    if args.odd_lot:
        use_odd_lot = True
    elif args.no_odd_lot:
        use_odd_lot = False

    fund_per_symbol = args.total_fund / max(1, args.top_n)
    overrides: dict = {"_env_file": None, "max_fund": int(fund_per_symbol)}
    if use_odd_lot is True:
        overrides["use_odd_lot"] = True
    elif use_odd_lot is False:
        overrides["use_odd_lot"] = False
    if args.tiers:
        overrides["smile_buy_tiers"] = args.tiers
    settings = Settings(**overrides)  # type: ignore[call-arg]

    db = StockDB.open(path=default_db_path(root))
    universe = collect_universe_tickers(
        root,
        min_etf_count=args.min_etf,
        universe_limit=args.universe_limit,
        extra_symbols=extra,
    )
    if args.fetch and universe:
        _ensure_daily_data(universe, start, end, root=root, db=db, logger=logger)

    report = screen_smile_candidates(
        db,
        root=root,
        start=start,
        end=end,
        total_fund=float(args.total_fund),
        top_n=args.top_n,
        min_etf_count=args.min_etf,
        max_price=args.max_price,
        min_ann_vol=args.min_vol,
        use_odd_lot=use_odd_lot,
        universe_limit=args.universe_limit,
        extra_symbols=extra,
        settings=settings,
        publisher=get_or_create_bus(),
    )

    if args.json or args.output:
        payload = {
            "start": report.start,
            "end": report.end,
            "total_fund": report.total_fund,
            "fund_per_symbol": report.fund_per_symbol,
            "top_n": report.top_n,
            "universe_size": report.universe_size,
            "filtered_size": report.filtered_size,
            "notes": report.notes,
            "selected": [c.to_dict() for c in report.selected],
            "candidates": [c.to_dict() for c in report.candidates],
            "settings_suggestion": report.settings_suggestion,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        if args.output:
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("已匯出 %s", out)
    else:
        _print_report(report)

    if not report.candidates and not extra:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

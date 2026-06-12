"""stock-smile-audit -- 微笑曲線多情境完整回測審計。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from bot.app_bootstrap import get_or_create_bus
from bot.events import SmileAuditCompleted
from bot.events.wiring import publish_if_bus
from bot.smile_curve_backtest_analysis import DEFAULT_SCENARIOS, audit_symbols
from bot.smile_curve_backtest_cli import _ensure_daily_data, _normalize_symbol, _resolve_dates
from bot.stock_db import StockDB, default_db_path
from bot.utils import get_logger, mk_folder, now_tw


def _print_audit(reports) -> None:
    print("\n=== 微笑曲線完整回測審計 ===")
    print(f"產生時間: {now_tw().isoformat(timespec='seconds')}")
    current_sym = ""
    for r in reports:
        if r.skip_reason:
            print(f"\n[{r.symbol}] {r.scenario.label}: 略過 ({r.skip_reason})")
            continue
        if r.symbol != current_sym:
            current_sym = r.symbol
            print(f"\n{'='*60}")
            print(f"  {r.symbol} {r.name}")
            print(f"  資料 {r.first_date} ~ {r.last_date} ({r.bar_count} 根日K)")
            print(f"  買進持有報酬: {r.buy_hold_return_pct:+.1f}%")
        sc = r.scenario
        ts = r.trade_stats
        print(f"\n  >> {sc.label}")
        print(
            f"     模式={sc.price_mode} | 資金={sc.max_fund:,} | "
            f"零股={'是' if sc.use_odd_lot else '否'} | tiers={sc.smile_buy_tiers}",
        )
        print(
            f"     回合={ts.trade_count} | 勝率={ts.win_rate:.1f}% | "
            f"淨利={ts.total_pnl_twd:,.0f} | 均筆={ts.avg_pnl_twd:,.0f} | "
            f"持有天={ts.avg_hold_days:.1f}",
        )
        if ts.trade_count:
            print(
                f"     最大獲利={ts.max_win_twd:,.0f} | 最大虧損={ts.max_loss_twd:,.0f} | "
                f"均報酬={ts.avg_pnl_pct:.2f}%",
            )
        if r.open_lots:
            print(
                f"     未平倉={r.open_lots} 筆 | 成本={r.open_lots_cost_twd:,.0f} | "
                f"期末浮動={r.open_lots_mtm_pnl_twd:+,.0f}",
            )
        for note in r.notes:
            print(f"     ※ {note}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="微笑曲線多情境完整回測審計")
    parser.add_argument("--symbols", required=True, help="逗號分隔，例 2303,2344")
    parser.add_argument("--start", help="覆寫所有情境起始日（可選）")
    parser.add_argument("--end", help="覆寫所有情境結束日（可選）")
    parser.add_argument("--fetch", action="store_true", help="缺日K時自動抓取")
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    parser.add_argument("--output", help="匯出 JSON 路徑")
    parser.add_argument(
        "--trades-csv",
        help="匯出所有回合明細 CSV（含 scenario 欄）",
    )
    args = parser.parse_args(argv)

    logger = get_logger("smile-audit")
    root = Path.cwd()
    symbols = [_normalize_symbol(s) for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        logger.error("請指定 --symbols")
        return 1

    scenarios = list(DEFAULT_SCENARIOS)
    if args.start or args.end:
        start, end = _resolve_dates(900, args.start, args.end)
        for sc in scenarios:
            if args.start:
                sc.start = start
            if args.end:
                sc.end = end

    db = StockDB.open(path=default_db_path(root))
    names: dict[str, str] = {}
    for sym in symbols:
        info = db.get_stock_info(sym)
        if info and info.name:
            short = info.name.replace("股份有限公司", "").replace("股份有限", "")
            names[sym] = short

    if args.fetch:
        earliest = min(sc.start for sc in scenarios)
        latest = max(sc.end for sc in scenarios)
        _ensure_daily_data(symbols, earliest, latest, root=root, db=db, logger=logger)

    reports = audit_symbols(db, symbols, scenarios, names=names)
    output_path = str(args.output) if args.output else None
    publish_if_bus(
        get_or_create_bus(),
        SmileAuditCompleted(
            symbols=tuple(symbols),
            scenario_count=len(scenarios),
            report_path=output_path,
        ),
    )

    if args.json or args.output:
        payload = {
            "generated_at": now_tw().isoformat(timespec="seconds"),
            "symbols": symbols,
            "reports": [r.to_dict() for r in reports],
            "trades": [
                {
                    "scenario": r.scenario.label,
                    "symbol": t.symbol,
                    "entry_date": t.entry_date,
                    "exit_date": t.exit_date,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "quantity": t.quantity,
                    "unit": t.unit,
                    "pnl_twd": round(t.pnl_twd, 2),
                    "pnl_pct": round(t.pnl_pct, 4),
                    "entry_reason": t.entry_reason,
                }
                for r in reports for t in r.round_trips
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.json:
            print(text)
        if args.output:
            out = Path(args.output)
            mk_folder(out.parent)
            out.write_text(text, encoding="utf-8")
            logger.info("已匯出 %s", out)

    if args.trades_csv:
        import csv
        out = Path(args.trades_csv)
        mk_folder(out.parent)
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([
                "scenario", "symbol", "entry_date", "exit_date",
                "entry_price", "exit_price", "quantity", "unit",
                "pnl_twd", "pnl_pct", "entry_reason", "reference",
            ])
            for r in reports:
                for t in r.round_trips:
                    w.writerow([
                        r.scenario.label, t.symbol, t.entry_date, t.exit_date,
                        f"{t.entry_price:.4f}", f"{t.exit_price:.4f}",
                        t.quantity, t.unit,
                        f"{t.pnl_twd:.2f}", f"{t.pnl_pct:.4f}",
                        t.entry_reason, f"{t.reference:.4f}",
                    ])
        logger.info("已匯出明細 %s", out)

    if not args.json:
        _print_audit(reports)

    return 0


if __name__ == "__main__":
    sys.exit(main())

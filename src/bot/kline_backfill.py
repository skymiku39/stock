"""回補回測標的的長期日 K，並驗證既有資料沒有被改動、列出缺口。

用法（先在 work/ 的副本驗證，確認後才對正式資料庫執行，見 docs/operations.md）：
    python -m bot.kline_backfill --root work/long-history-17/root

每檔每月一次請求，間隔預設 3 秒；抓不到的月份留為缺口，重跑時只補缺的月份。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from bot.backtest_universe import load_universe


def snapshot_rows(db, symbols: Sequence[str]) -> Dict[Tuple[str, str], Tuple[float, float, float, float, float]]:
    out = {}
    for sym in symbols:
        for b in db.get_price_history(sym, ascending=True):
            out[(sym, b.date)] = (b.open, b.high, b.low, b.close, b.volume)
    return out


def compare_snapshots(before: dict, after: dict) -> dict:
    """既有的 (代號, 日期) 在回補後必須仍存在且數值完全相同。"""
    missing = [k for k in before if k not in after]
    changed = [k for k in before if k in after and after[k] != before[k]]
    return {
        "before_rows": len(before),
        "after_rows": len(after),
        "missing_after": [list(k) for k in missing],
        "changed": [list(k) for k in changed],
        "unchanged": not missing and not changed,
    }


def month_gaps(dates: Sequence[str], end: dt.date) -> List[str]:
    """從第一筆資料所在月份到 end 所在月份之間，完全沒有資料的月份。"""
    if not dates:
        return []
    have = {d[:7] for d in dates}
    first = dt.date.fromisoformat(min(dates)[:10]).replace(day=1)
    gaps, cur = [], first
    while cur <= end:
        key = f"{cur.year}-{cur.month:02d}"
        if key not in have:
            gaps.append(key)
        cur = dt.date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
    return gaps


def coverage_report(db, symbols: Sequence[str], start: dt.date, end: dt.date) -> List[dict]:
    rows = []
    for sym in symbols:
        dates = [b.date for b in db.get_price_history(sym, ascending=True)]
        in_range = [d for d in dates if d >= start.isoformat()]
        first = min(in_range) if in_range else ""
        late = bool(first) and first[:7] > start.isoformat()[:7]
        rows.append({
            "symbol": sym,
            "rows": len(in_range),
            "first_date": first,
            "last_date": max(in_range) if in_range else "",
            "starts_after_range_start": late,
            "missing_months": month_gaps(in_range, end),
            "note": (
                "沒有任何資料" if not in_range
                else "起始晚於回補起點（上市日之後才有資料，或來源缺漏）" if late else ""
            ),
        })
    return rows


def run(
    root: Path,
    symbols: Sequence[str],
    *,
    start: dt.date,
    end: dt.date,
    delay: float,
    fetch=None,
    on_symbol=None,
) -> dict:
    from bot.stock_db import StockDB, default_db_path
    from bot.technicals import fetch_kline_range

    fetch = fetch or fetch_kline_range
    db = StockDB.open(path=default_db_path(root))
    try:
        before = snapshot_rows(db, symbols)
    finally:
        db.close()
    for i, sym in enumerate(symbols, start=1):
        fetch(sym, start_date=start, end_date=end, root=root,
              request_delay_sec=delay, direction="forward")
        if on_symbol:
            on_symbol(i, len(symbols), sym)
    db = StockDB.open(path=default_db_path(root))
    try:
        after = snapshot_rows(db, symbols)
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "symbols": len(symbols),
            "integrity": compare_snapshots(before, after),
            "coverage": coverage_report(db, symbols, start, end),
        }
    finally:
        db.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="回補回測標的日 K")
    parser.add_argument("--root", required=True, help="專案根目錄或其副本（內含 data/stock.db）")
    parser.add_argument("--universe", default="config/backtest_universe.json")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="")
    parser.add_argument("--delay", type=float, default=3.0)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    report = run(
        Path(args.root), load_universe(Path(args.universe)),
        start=dt.date.fromisoformat(args.start), end=end, delay=args.delay,
        on_symbol=lambda i, n, s: print(f"[{i}/{n}] {s}", flush=True),
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(f"既有資料不變：{report['integrity']['unchanged']}；"
          f"有缺口的標的：{sum(1 for c in report['coverage'] if c['missing_months'] or c['note'])}")
    return 0 if report["integrity"]["unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

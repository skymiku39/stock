"""FinMind 台股資料交叉核對：月營收、損益表關鍵科目 vs. 本地資料庫。

規格見 docs/free-sources/finmind.md。資料來源：FinMind（https://finmindtrade.com）。
唯讀開啟資料庫；報告寫到 root/data/reports/finmind_crosscheck_<台灣日期>.json。

用法：
    python -m bot.free_sources.finmind_crosscheck [--root .] [--db 路徑] [--symbols 2330,1514] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.free_sources.base import SourceError, redact
from bot.free_sources.finmind import FinMindClient
from bot.free_sources.quota import QuotaExceeded, default_quota
from bot.runtime_state import atomic_json
from bot.utils import get_logger, now_tw

log = get_logger(__name__)

SOURCE_NAME = "FinMind"
REVENUE_DATASET = "TaiwanStockMonthRevenue"
STATEMENT_DATASET = "TaiwanStockFinancialStatements"
MONTHS = 3
QUARTERS = 4

# 容許誤差（見文件）
REVENUE_REL_TOL = 0.001      # 營收：相對 0.1%
EPS_ABS_TOL = 0.05           # EPS：絕對 0.05 元，或相對 0.5%（取較大者）
EPS_REL_TOL = 0.005
MARGIN_ABS_TOL = 0.05        # 利潤率：0.05 個百分點

MATCH, MISMATCH, DB_MISSING, SOURCE_MISSING = "match", "mismatch", "db_missing", "source_missing"


def report_path(root: Path, day: dt.date) -> Path:
    return Path(root) / "data" / "reports" / f"finmind_crosscheck_{day.isoformat()}.json"


# ---------------- 資料庫（唯讀） ----------------
def _open_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=30)


def _db_rows(con: sqlite3.Connection, sql: str, params: tuple) -> List[tuple]:
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


# ---------------- 比對 ----------------
def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _item(kind: str, symbol: str, period: str, field: str, db: Optional[float],
          fm: Optional[float], tol: float, unit: str, rel: float = 0.0) -> dict:
    """db / fm 已換算成同一單位。db 為空或 0 而 FinMind 有值 → db_missing。"""
    if fm is None:
        status, diff = SOURCE_MISSING, None
    elif db is None or (db == 0 and fm != 0):
        status, diff = DB_MISSING, None
    else:
        diff = db - fm
        status = MATCH if abs(diff) <= max(tol, rel * abs(fm)) else MISMATCH
    return {"kind": kind, "symbol": symbol, "period": period, "field": field,
            "db": db, "finmind": fm, "diff": None if diff is None else round(diff, 4),
            "unit": unit, "status": status}


def compare_revenue(symbol: str, rows: List[dict], db_rows: Dict[str, float]) -> List[dict]:
    """FinMind 月營收（元）對資料庫 monthly_revenue（千元）；取 FinMind 最新 MONTHS 個月。"""
    by_month: Dict[str, float] = {}
    for r in rows:
        try:
            ym = f"{int(r['revenue_year']):04d}-{int(r['revenue_month']):02d}"
        except (KeyError, TypeError, ValueError):
            continue
        v = _num(r.get("revenue"))
        if v is not None:
            by_month[ym] = v
    out = []
    for ym in sorted(by_month, reverse=True)[:MONTHS]:
        fm = by_month[ym] / 1000.0   # 元 -> 千元
        out.append(_item("monthly_revenue", symbol, ym, "revenue", db_rows.get(ym), fm,
                         0.0, "千元", REVENUE_REL_TOL))
    return out


def _quarter_label(date: str) -> Optional[tuple]:
    try:
        year, month = int(date[:4]), int(date[5:7])
    except (TypeError, ValueError):
        return None
    q = {3: 1, 6: 2, 9: 3, 12: 4}.get(month)
    return (year, q) if q else None


def compare_statements(symbol: str, rows: List[dict], db_rows: Dict[str, dict]) -> List[dict]:
    """FinMind 單季損益表 -> 年初累計，對資料庫 quarterly_report（累計、營收千元）。

    比對 revenue、eps、op_margin（OperatingIncome / Revenue）。累計所需的季別 FinMind 缺任何一季
    就標 source_missing（無法驗證）。
    """
    table: Dict[tuple, Dict[str, float]] = {}
    for r in rows:
        key = _quarter_label(str(r.get("date") or ""))
        v = _num(r.get("value"))
        if key and v is not None and r.get("type"):
            table.setdefault(key, {})[str(r["type"])] = v
    quarters = sorted((k for k, v in table.items() if "Revenue" in v or "EPS" in v), reverse=True)[:QUARTERS]
    out = []
    for year, q in quarters:
        period = f"{year}Q{q}"
        parts = [table.get((year, i)) for i in range(1, q + 1)]
        complete = all(parts)

        def ytd(name: str) -> Optional[float]:
            if not complete or any(name not in p for p in parts):
                return None
            return sum(p[name] for p in parts)

        db = db_rows.get(period)
        rev, eps, op = ytd("Revenue"), ytd("EPS"), ytd("OperatingIncome")
        out.append(_item("quarterly_report", symbol, period, "revenue",
                         db and db["revenue"], None if rev is None else rev / 1000.0, 0.0, "千元(累計)", REVENUE_REL_TOL))
        out.append(_item("quarterly_report", symbol, period, "eps",
                         db and db["eps"], eps, EPS_ABS_TOL, "元(累計)", EPS_REL_TOL))
        margin = op / rev * 100 if (op is not None and rev) else None
        out.append(_item("quarterly_report", symbol, period, "op_margin",
                         db and db["op_margin"], margin, MARGIN_ABS_TOL, "%(累計)"))
    return out


def summarize(items: List[dict]) -> Dict[str, int]:
    s = {MATCH: 0, MISMATCH: 0, DB_MISSING: 0, SOURCE_MISSING: 0}
    for it in items:
        s[it["status"]] += 1
    s["total"] = len(items)
    return s


# ---------------- 主流程 ----------------
def default_symbols(root: Path) -> List[str]:
    from bot.held_symbols import load_held_symbols
    return load_held_symbols(root)


def run_crosscheck(
    root: Path,
    *,
    now: Optional[dt.datetime] = None,
    settings: Any = None,
    client: Optional[FinMindClient] = None,
    symbols: Optional[List[str]] = None,
    db_path: Optional[Path] = None,
    dry_run: bool = False,
) -> dict:
    root = Path(root)
    now = now or now_tw()
    today = now.date()
    syms = list(dict.fromkeys(symbols if symbols is not None else default_symbols(root)))
    db_path = Path(db_path) if db_path else root / "data" / "stock.db"
    path = report_path(root, today)
    start_rev = (today.replace(day=1) - dt.timedelta(days=MONTHS * 31 + 31)).replace(day=1)
    start_stmt = dt.date(today.year - 2, 1, 1)

    if dry_run:
        return {"status": "dry_run", "source": SOURCE_NAME, "symbols": syms,
                "planned_requests": len(syms) * 2, "db": str(db_path), "path": str(path)}

    if client is None:
        if settings is None:
            from bot.config import Settings
            env_file = root / ".env"
            settings = Settings(_env_file=str(env_file)) if env_file.exists() else Settings()
        key = str(getattr(settings, "finmind_token", "") or "").strip()
        if not key:
            return {"status": "skipped", "source": SOURCE_NAME, "reason": "no FINMIND_TOKEN"}
        client = FinMindClient(key, quota=default_quota("finmind", directory=root / "data" / "api_quota"))

    items: List[dict] = []
    errors: Dict[str, str] = {}
    requests_made = 0
    status = "ok"
    try:
        con = _open_ro(db_path)
    except sqlite3.Error as exc:
        return {"status": "error", "source": SOURCE_NAME, "errors": {"db": str(exc)}, "symbols": syms}
    try:
        for sym in syms:
            db_rev = {str(r[0]): _num(r[1]) for r in _db_rows(
                con, "SELECT year_month, revenue FROM monthly_revenue WHERE symbol=?", (sym,))}
            db_rev = {k.replace("/", "-")[:7]: v for k, v in db_rev.items()}
            db_q = {str(r[0]): {"eps": _num(r[1]), "revenue": _num(r[2]), "op_margin": _num(r[3])}
                    for r in _db_rows(con, "SELECT period, eps, revenue, op_margin FROM quarterly_report WHERE symbol=?", (sym,))}
            plan = (
                ("revenue", REVENUE_DATASET, start_rev, lambda rows: compare_revenue(sym, rows, db_rev)),
                ("statements", STATEMENT_DATASET, start_stmt, lambda rows: compare_statements(sym, rows, db_q)),
            )
            for label, dataset, start, fn in plan:
                try:
                    requests_made += 1
                    rows = client.data(dataset, sym, start.isoformat(), today.isoformat())
                except QuotaExceeded as exc:
                    requests_made -= 1
                    status = "quota_exceeded"
                    errors[f"{sym}:{label}"] = str(exc)
                    break
                except SourceError as exc:
                    errors[f"{sym}:{label}"] = redact(f"{exc.kind}: {exc.message}")
                    log.warning("finmind crosscheck %s %s failed: %s", sym, label, exc.kind)
                    if exc.kind == "quota_exhausted":
                        status = "quota_exceeded"
                        break
                    continue
                items.extend(fn(rows))
            if status == "quota_exceeded":
                break
    finally:
        con.close()

    if status == "ok" and errors:
        status = "partial" if items else "error"
    counts = summarize(items)
    report = {
        "status": status,
        "source": SOURCE_NAME,
        "source_url": "https://finmindtrade.com",
        "generated_at": now.isoformat(timespec="seconds"),
        "date": today.isoformat(),
        "symbols": syms,
        "requests": requests_made,
        "summary": counts,
        "inconsistent": counts[MISMATCH] > 0,
        "has_db_missing": counts[DB_MISSING] > 0,
        "errors": errors,
        "items": items,
    }
    atomic_json(path, report)
    report["path"] = str(path)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="FinMind 台股資料交叉核對（月營收、損益表）")
    ap.add_argument("--root", default=".", help="專案根目錄（報告與用量檔寫在這裡）")
    ap.add_argument("--db", default=None, help="資料庫路徑（預設 <root>/data/stock.db；唯讀開啟）")
    ap.add_argument("--symbols", default=None, help="逗號分隔代號；預設用持股名單")
    ap.add_argument("--dry-run", action="store_true", help="只列出計畫，不連網、不寫檔")
    args = ap.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    result = run_crosscheck(Path(args.root), symbols=symbols,
                            db_path=Path(args.db) if args.db else None, dry_run=args.dry_run)
    result.pop("items", None) if args.dry_run else None
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") in ("error", "quota_exceeded") else 0


if __name__ == "__main__":
    sys.exit(main())

"""FMP 美股財報收集：每週一次抓損益表 / 資產負債表 / 現金流量表（年 + 季）。

用法：uv run --offline --no-sync python -m bot.free_sources.collect_fmp
        [--symbols NVDA,TSM] [--force] [--dry-run]
只當研究背景資料，不進任何交易規則。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.free_sources.base import SourceError
from bot.free_sources.fmp import BASE_URL, FmpClient
from bot.free_sources.quota import TAIPEI, QuotaExceeded

log = logging.getLogger(__name__)

ANNUAL_LIMIT = 5
QUARTER_LIMIT = 5  # 免費版 limit 上限 5（實測 limit=8 會被拒）
# 報表鍵 -> (FmpClient 方法, 端點路徑)
STATEMENTS = {
    "income": ("income_statement", "income-statement"),
    "balance": ("balance_sheet_statement", "balance-sheet-statement"),
    "cashflow": ("cash_flow_statement", "cash-flow-statement"),
}
PERIODS = {"annual": ("annual", ANNUAL_LIMIT), "quarter": ("quarter", QUARTER_LIMIT)}
REQUESTS_PER_SYMBOL = len(STATEMENTS) * len(PERIODS)


def default_symbols() -> List[str]:
    from bot.market_macro import DEFAULT_STOCKS

    return [row[0] for row in DEFAULT_STOCKS]


def data_dir(root: Path) -> Path:
    return Path(root) / "data" / "us_fundamentals"


def _now() -> dt.datetime:
    return dt.datetime.now(TAIPEI)


def _iso_week(value: dt.datetime) -> tuple:
    iso = value.astimezone(TAIPEI).isocalendar()
    return (iso[0], iso[1])


def fetched_this_week(path: Path, now: Optional[dt.datetime] = None) -> bool:
    """檔內 fetched_at 與 now 是否同屬台灣時間的同一個 ISO 週。"""
    now = now or _now()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        stamp = dt.datetime.fromisoformat(str(data["fetched_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TAIPEI)
    return _iso_week(stamp) == _iso_week(now)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sorted_desc(rows: List[dict]) -> List[dict]:
    return sorted(rows, key=lambda r: str(r.get("date") or ""), reverse=True)


def _register_raw(registry: Any, symbol: str, stmt: str, period: str, rows: List[dict]) -> None:
    """原始回應存進 SourceRegistry（內容為回應本體，不含 URL 查詢字串與金鑰）。"""
    if registry is None:
        return
    try:
        registry.register_document(
            "fmp",
            json.dumps(rows, ensure_ascii=False, sort_keys=True),
            kind="raw",
            source_ref=f"{symbol}:{stmt}:{period}",
            url=f"{BASE_URL}/{STATEMENTS[stmt][1]}?symbol={symbol}&period={period}",
            suffix="json",
            mime_type="application/json",
            as_of=str(rows[0].get("date") or "") if rows else "",
            metadata={"symbol": symbol, "statement": stmt, "period": period},
        )
    except Exception as exc:  # 登錄失敗不阻擋 JSON 輸出
        log.warning("fmp raw register failed %s %s %s: %s", symbol, stmt, period, type(exc).__name__)


def fetch_symbol(client: Any, symbol: str, registry: Any = None) -> dict:
    """抓一檔的 6 份報表；任何一次失敗就 raise（呼叫端決定是否寫檔）。"""
    statements: Dict[str, Dict[str, List[dict]]] = {}
    for stmt, (method, _path) in STATEMENTS.items():
        statements[stmt] = {}
        for label, (period, limit) in PERIODS.items():
            rows = getattr(client, method)(symbol, period=period, limit=limit)
            rows = _sorted_desc([r for r in rows if isinstance(r, dict)])
            _register_raw(registry, symbol, stmt, label, rows)
            statements[stmt][label] = rows
    currency = ""
    for stmt in ("income", "balance", "cashflow"):
        for label in ("annual", "quarter"):
            rows = statements[stmt][label]
            if rows and rows[0].get("reportedCurrency"):
                currency = str(rows[0]["reportedCurrency"])
                break
        if currency:
            break
    return {"symbol": symbol, "currency": currency, "statements": statements}


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _series(rows: List[dict], key: str) -> List[dict]:
    out = []
    for r in rows:
        src = r
        out.append({"date": r.get("date"), "value": _num(src.get(key))})
    return out


def _pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return round((new - old) / abs(old) * 100, 2)


def _summ_period(income: List[dict], cashflow: List[dict], yoy_gap: int) -> dict:
    revenue = _series(income, "revenue")
    eps = _series(income, "epsDiluted")
    if all(p["value"] is None for p in eps):
        eps = _series(income, "eps")
    margin = []
    for r in income:
        rev, gp = _num(r.get("revenue")), _num(r.get("grossProfit"))
        margin.append({
            "date": r.get("date"),
            "value": round(gp / rev * 100, 2) if rev and gp is not None else None,
        })
    fcf = _series(cashflow, "freeCashFlow")

    def latest(s: List[dict]) -> Optional[float]:
        return s[0]["value"] if s else None

    def yoy(s: List[dict]) -> Optional[float]:
        return _pct_change(latest(s), s[yoy_gap]["value"]) if len(s) > yoy_gap else None

    return {
        "latest_date": income[0].get("date") if income else None,
        "revenue": {"latest": latest(revenue), "yoy_pct": yoy(revenue), "series": revenue},
        "gross_margin_pct": {
            "latest": latest(margin),
            "change_pts": (
                round(margin[0]["value"] - margin[yoy_gap]["value"], 2)
                if len(margin) > yoy_gap and margin[0]["value"] is not None
                and margin[yoy_gap]["value"] is not None else None
            ),
            "series": margin,
        },
        "eps": {"latest": latest(eps), "yoy_pct": yoy(eps), "series": eps},
        "free_cash_flow": {"latest": latest(fcf), "yoy_pct": yoy(fcf), "series": fcf},
    }


def summarize(symbol_data: dict) -> dict:
    """趨勢摘要。年報比上一年（gap=1）、季報比去年同季（gap=4）。
    金額以 currency 計、不換算；毛利率與年增率與幣別無關。"""
    st = (symbol_data or {}).get("statements") or {}

    def rows(stmt: str, label: str) -> List[dict]:
        return _sorted_desc(list((st.get(stmt) or {}).get(label) or []))

    return {
        "symbol": (symbol_data or {}).get("symbol"),
        "currency": (symbol_data or {}).get("currency") or "",
        "fetched_at": (symbol_data or {}).get("fetched_at"),
        "annual": _summ_period(rows("income", "annual"), rows("cashflow", "annual"), 1),
        "quarter": _summ_period(rows("income", "quarter"), rows("cashflow", "quarter"), 4),
    }


def collect(
    root: Path,
    client: Any,
    symbols: Optional[List[str]] = None,
    *,
    force: bool = False,
    dry_run: bool = False,
    now: Optional[dt.datetime] = None,
    registry: Any = None,
) -> dict:
    """回傳 {'fetched','skipped_week','errors','quota_stopped','requests_planned', ...}。"""
    symbols = [s.strip().upper() for s in (symbols or default_symbols()) if s.strip()]
    now = now or _now()
    res: Dict[str, Any] = {
        "fetched": [], "skipped_week": [], "errors": {}, "quota_stopped": False,
        "not_attempted": [], "requests_planned": 0, "dry_run": dry_run,
    }
    todo = []
    for sym in symbols:
        path = data_dir(root) / f"{sym}.json"
        if not force and fetched_this_week(path, now):
            res["skipped_week"].append(sym)
        else:
            todo.append(sym)
    res["requests_planned"] = len(todo) * REQUESTS_PER_SYMBOL
    if dry_run:
        res["would_fetch"] = todo
        return res
    for i, sym in enumerate(todo):
        try:
            data = fetch_symbol(client, sym, registry)
        except QuotaExceeded as exc:
            res["quota_stopped"] = True
            res["not_attempted"] = todo[i:]
            res["errors"][sym] = f"quota: {exc}"
            break
        except SourceError as exc:
            res["errors"][sym] = f"{exc.kind}: {exc.message}"
            continue
        except Exception as exc:
            res["errors"][sym] = type(exc).__name__
            continue
        data["fetched_at"] = now.astimezone(TAIPEI).isoformat(timespec="seconds")
        _atomic_write_json(data_dir(root) / f"{sym}.json", data)
        res["fetched"].append(sym)
    return res


def main(argv: Optional[List[str]] = None, *, root: Optional[Path] = None, session: Any = None) -> int:
    ap = argparse.ArgumentParser(description="FMP 美股財報收集")
    ap.add_argument("--symbols", help="逗號分隔；預設 DEFAULT_STOCKS 全部")
    ap.add_argument("--force", action="store_true", help="即使本週已抓過也重抓")
    ap.add_argument("--dry-run", action="store_true", help="只列出計畫，不請求、不寫檔")
    ap.add_argument("--root", help="輸出根目錄（預設目前目錄）")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else (root or Path.cwd())
    symbols = args.symbols.split(",") if args.symbols else None

    if args.dry_run:
        res = collect(root, None, symbols, force=args.force, dry_run=True)
        print(json.dumps(res, ensure_ascii=False))
        return 0

    from bot.config import Settings

    key = str(getattr(Settings(), "fmp_api_key", "") or "").strip()
    if not key:
        print("fmp: skipped (no FMP_API_KEY)")
        return 0
    from bot.source_registry import SourceRegistry

    client = FmpClient(key, session=session)
    registry = None
    try:
        registry = SourceRegistry(root)
    except Exception as exc:
        log.warning("SourceRegistry unavailable: %s", type(exc).__name__)
    try:
        res = collect(root, client, symbols, force=args.force, registry=registry)
    finally:
        if registry is not None:
            registry.close()
    print(json.dumps(res, ensure_ascii=False))
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

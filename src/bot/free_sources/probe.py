"""探測指令：每家打一次最小請求，回報狀態 / 筆數 / 第一筆欄位名（不印內容、不印金鑰）。

用法：uv run --offline --no-sync python -m bot.free_sources.probe [--source finnhub ...] [--json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from bot.free_sources.alphavantage import AlphaVantageClient
from bot.free_sources.base import BaseClient, SourceError
from bot.free_sources.finmind import FinMindClient
from bot.free_sources.finnhub import FinnhubClient
from bot.free_sources.fmp import FmpClient
from bot.free_sources.fugle import FugleClient
from bot.free_sources.marketaux import MarketauxClient
from bot.free_sources.massive import MassiveClient
from bot.free_sources.quota import TAIPEI, QuotaExceeded, default_quota

# source -> (Settings 欄位, client 類別)
SOURCES: Dict[str, tuple] = {
    "finnhub": ("finnhub_api_key", FinnhubClient),
    "fugle": ("fugle_api_key", FugleClient),
    "finmind": ("finmind_token", FinMindClient),
    "fmp": ("fmp_api_key", FmpClient),
    "massive": ("massive_api_key", MassiveClient),
    "marketaux": ("marketaux_api_token", MarketauxClient),
    "alphavantage": ("alphavantage_api_key", AlphaVantageClient),
}


def _keys(obj: Any) -> List[str]:
    return sorted(obj.keys()) if isinstance(obj, dict) else []


def _first(rows: Any) -> Any:
    return rows[0] if isinstance(rows, list) and rows else None


def _probe_finnhub(c: FinnhubClient, today: dt.date) -> dict:
    rows = c.company_news("NVDA", str(today - dt.timedelta(days=3)), str(today))
    return {"count": len(rows), "first_keys": _keys(_first(rows))}


def _probe_fugle(c: FugleClient, today: dt.date) -> dict:
    q = c.intraday_quote("2330")
    return {"count": 1 if q else 0, "first_keys": _keys(q)}


def _probe_finmind(c: FinMindClient, today: dt.date) -> dict:
    start = str(today - dt.timedelta(days=92))
    rows = c.data("TaiwanStockMonthRevenue", "2330", start, str(today))
    out = {"count": len(rows), "first_keys": _keys(_first(rows))}
    info = c.user_info()
    out["extra"] = {k: info.get(k) for k in ("user_count", "api_request_limit") if isinstance(info, dict)}
    return out


def _probe_fmp(c: FmpClient, today: dt.date) -> dict:
    rows = c.income_statement("AAPL", "annual", 1)
    return {"count": len(rows), "first_keys": _keys(_first(rows))}


def _probe_massive(c: MassiveClient, today: dt.date) -> dict:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = c.news(published_gte=since, limit=5)
    rows = body.get("results") if isinstance(body, dict) else None
    return {"count": len(rows or []), "first_keys": _keys(_first(rows))}


def _probe_marketaux(c: MarketauxClient, today: dt.date) -> dict:
    after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    body = c.news_all("TSM", after)
    rows = body.get("data") if isinstance(body, dict) else None
    return {"count": len(rows or []), "first_keys": _keys(_first(rows))}


def _probe_alphavantage(c: AlphaVantageClient, today: dt.date) -> dict:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    body = c.news_sentiment("financial_markets", since, limit=5)
    rows = body.get("feed")
    return {"count": len(rows or []), "first_keys": _keys(_first(rows))}


_PROBES: Dict[str, Callable[[Any, dt.date], dict]] = {
    "finnhub": _probe_finnhub,
    "fugle": _probe_fugle,
    "finmind": _probe_finmind,
    "fmp": _probe_fmp,
    "massive": _probe_massive,
    "marketaux": _probe_marketaux,
    "alphavantage": _probe_alphavantage,
}


def run_probe(
    settings: Any,
    sources: Optional[List[str]] = None,
    session: Any = None,
    quota_dir: Optional[Path] = None,
    today: Optional[dt.date] = None,
) -> List[dict]:
    today = today or dt.datetime.now(TAIPEI).date()
    results: List[dict] = []
    for name in sources or list(SOURCES):
        field, cls = SOURCES[name]
        key = str(getattr(settings, field, "") or "").strip()
        rec: Dict[str, Any] = {"source": name, "status": "ok", "http_status": None,
                               "count": None, "first_keys": [], "error": None}
        if not key:
            rec["status"] = "skipped"
            results.append(rec)
            continue
        client: BaseClient = cls(key, session=session, quota=default_quota(name, quota_dir))
        try:
            rec.update(_PROBES[name](client, today))
        except QuotaExceeded as exc:
            rec.update(status="error", error="quota_exceeded", message=str(exc))
        except SourceError as exc:
            rec.update(status="error", error=exc.kind, message=exc.message, http_status=exc.status)
        except Exception as exc:  # 探測不可因單一來源崩潰
            rec.update(status="error", error=type(exc).__name__)
        if rec["http_status"] is None:
            rec["http_status"] = client.last_status
        results.append(rec)
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="免費資料來源探測")
    ap.add_argument("--source", nargs="+", choices=sorted(SOURCES), help="只探測指定來源")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    args = ap.parse_args(argv)

    from bot.config import Settings

    results = run_probe(Settings(), args.source)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            line = f"{r['source']:<13}{r['status']:<8}"
            if r["status"] != "skipped":
                line += f" http={r['http_status']} count={r['count']} keys={','.join(r['first_keys'])}"
                if r.get("error"):
                    line += f" error={r['error']}"
            print(line)
    return 1 if any(r["status"] == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())

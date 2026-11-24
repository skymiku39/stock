"""Finnhub 收集程式：美股公司新聞 + 大盤新聞 -> data/news/api_finnhub_<台灣日期>.json。

用法：uv run --offline --no-sync python -m bot.free_sources.collect_finnhub [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from bot.free_sources.base import SourceError, redact
from bot.free_sources.finnhub import FinnhubClient, normalize_news
from bot.free_sources.quota import TAIPEI, QuotaExceeded, default_quota
from bot.runtime_state import atomic_json, read_json
from bot.utils import get_logger

log = get_logger(__name__)


def default_symbols() -> List[str]:
    """DEFAULT_STOCKS 中的美股代號（排除純數字的台股代號）。"""
    from bot.market_macro import DEFAULT_STOCKS

    return [s[0] for s in DEFAULT_STOCKS if s[0] and not str(s[0]).isdigit()]


def previous_weekday(day: dt.date) -> dt.date:
    d = day - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def output_path(root: Path, day: dt.date) -> Path:
    return Path(root) / "data" / "news" / f"api_finnhub_{day.isoformat()}.json"


def _key(item: dict) -> str:
    return str(item.get("url") or "").strip() or str(item.get("title") or "").strip()


def merge_items(existing: Iterable[Any], new: Iterable[Any]) -> List[dict]:
    """既有在前、新的在後；以 url（無 url 則 title）去重，重複者的 us_tickers 併入保留的那則。"""
    kept: Dict[str, dict] = {}
    out: List[dict] = []
    for it in list(existing) + list(new):
        if not isinstance(it, dict):
            continue
        k = _key(it)
        if not k:
            continue
        if k in kept:
            first = kept[k]
            tickers = list(first.get("us_tickers") or [])
            for t in it.get("us_tickers") or []:
                if t not in tickers:
                    tickers.append(t)
            first["us_tickers"] = tickers
            continue
        it = dict(it)
        kept[k] = it
        out.append(it)
    return out


def collect(
    root: Path,
    *,
    now: dt.datetime,
    settings: Any = None,
    client: Optional[FinnhubClient] = None,
    symbols: Optional[List[str]] = None,
    dry_run: bool = False,
) -> dict:
    root = Path(root)
    today = now.astimezone(TAIPEI).date()
    start = previous_weekday(today)
    syms = [s.upper() for s in (symbols if symbols is not None else default_symbols())]

    if dry_run:
        return {"status": "dry_run", "symbols": syms, "from": str(start), "to": str(today),
                "planned_requests": len(syms) + 1, "path": str(output_path(root, today))}

    if client is None:
        if settings is None:
            from bot.config import Settings

            settings = Settings()
        key = str(getattr(settings, "finnhub_api_key", "") or "").strip()
        if not key:
            return {"status": "skipped"}
        client = FinnhubClient(
            key, quota=default_quota("finnhub", directory=root / "data" / "api_quota")
        )

    items: List[dict] = []
    counts: Dict[str, int] = {}
    errors: Dict[str, str] = {}
    requests_made = 0
    status = "ok"

    def run(label: str, fn, norm_symbol: Optional[str]) -> bool:
        nonlocal requests_made, status
        try:
            requests_made += 1
            rows = fn()
        except QuotaExceeded as exc:
            requests_made -= 1  # 未送出
            status = "quota_exceeded"
            errors[label] = str(exc)
            return False
        except SourceError as exc:
            errors[label] = f"{exc.kind}: {exc.message}"
            log.warning("finnhub %s failed: %s", label, exc.kind)
            return True
        got = normalize_news(rows, norm_symbol)
        counts[label] = len(got)
        items.extend(got)
        return True

    proceed = True
    for sym in syms:
        proceed = run(sym, lambda s=sym: client.company_news(s, str(start), str(today)), sym)
        if not proceed:
            break
    if proceed:
        run("general", client.general_news, None)

    path = output_path(root, today)
    existing = read_json(path, [])
    if not isinstance(existing, list):
        existing = []
    merged = merge_items(existing, items)
    wrote = False
    if items:  # 沒抓到任何新項目就不動既有檔
        atomic_json(path, merged)
        wrote = True
    if errors and status == "ok":
        status = "partial" if counts else "error"
    return {
        "status": status,
        "counts": counts,
        "errors": {k: redact(v) for k, v in errors.items()},
        "requests": requests_made,
        "new_items": len(items),
        "total_items": len(merged),
        "path": str(path) if wrote else None,
        "from": str(start),
        "to": str(today),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Finnhub 美股新聞收集")
    ap.add_argument("--dry-run", action="store_true", help="只列出將查詢的內容，不連網、不寫檔")
    ap.add_argument("--root", default=".", help="專案根目錄（預設目前目錄）")
    ap.add_argument("--symbols", nargs="+", help="只查指定代號")
    args = ap.parse_args(argv)
    summary = collect(
        Path(args.root),
        now=dt.datetime.now(TAIPEI),
        symbols=args.symbols,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary.get("status") in ("error", "quota_exceeded") else 0


if __name__ == "__main__":
    sys.exit(main())

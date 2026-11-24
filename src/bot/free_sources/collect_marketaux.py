"""Marketaux 收集程式：持股相關美股的「隔夜情緒」小表。

輸出：
- data/news/marketaux_sentiment_<台灣日期>.json  個股隔夜情緒（平均分數、則數、正/負則數）
- data/news/api_marketaux_<台灣日期>.json        正規化後的新聞（以 url 去重合併）

分數是 Marketaux 自己算的、方法不透明，只當參考，不進交易規則。

用法：uv run --offline --no-sync python -m bot.free_sources.collect_marketaux [--symbols TSM,NVDA] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from bot.free_sources.base import SourceError, redact
from bot.free_sources.marketaux import MarketauxClient, normalize_news
from bot.free_sources.quota import TAIPEI, QuotaExceeded, default_quota
from bot.runtime_state import atomic_json, read_json
from bot.utils import get_logger

log = get_logger(__name__)

DEFAULT_SYMBOLS = ["TSM", "NVDA", "AMD", "MU"]
MAX_PAGES = 2
PAGE_SIZE = 3  # 免費方案每次最多 3 則
NOTE = "分數由 Marketaux 計算、方法不透明，僅供參考，不進交易規則"


def previous_weekday(day: dt.date) -> dt.date:
    d = day - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def _us_close_utc(day: dt.date) -> dt.datetime:
    """該日美東 16:00 收盤對應的 UTC 時間。"""
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime(day.year, day.month, day.day, 16, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(
            dt.timezone.utc
        )
    except Exception:  # 沒有 tzdata：以美國夏令時間規則（3 月第二個週日～11 月第一個週日）估算
        def nth_sunday(y: int, m: int, n: int) -> dt.date:
            d = dt.date(y, m, 1)
            d += dt.timedelta(days=(6 - d.weekday()) % 7)
            return d + dt.timedelta(weeks=n - 1)

        dst = nth_sunday(day.year, 3, 2) <= day < nth_sunday(day.year, 11, 1)
        return dt.datetime(day.year, day.month, day.day, 20 if dst else 21, 0, tzinfo=dt.timezone.utc)


def published_after(today_tw: dt.date) -> str:
    """前一個平日美東收盤後（UTC，無時區字尾，格式如官方範例）。"""
    return _us_close_utc(previous_weekday(today_tw)).strftime("%Y-%m-%dT%H:%M:%S")


def news_path(root: Path, day: dt.date) -> Path:
    return Path(root) / "data" / "news" / f"api_marketaux_{day.isoformat()}.json"


def sentiment_path(root: Path, day: dt.date) -> Path:
    return Path(root) / "data" / "news" / f"marketaux_sentiment_{day.isoformat()}.json"


def _key(item: dict) -> str:
    return str(item.get("url") or "").strip() or str(item.get("title") or "").strip()


def merge_items(existing: Iterable[Any], new: Iterable[Any]) -> List[dict]:
    """既有在前、新的在後；以 url（無 url 則 title）去重。"""
    seen = set()
    out: List[dict] = []
    for it in list(existing) + list(new):
        if not isinstance(it, dict):
            continue
        k = _key(it)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _symbol_scores(body: Any, symbol: str) -> Dict[str, float]:
    """回傳 {文章識別: 該檔的 sentiment_score}；只取 entities 中 symbol 等於該檔且有數值分數者。"""
    out: Dict[str, float] = {}
    rows = body.get("data") if isinstance(body, dict) else None
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        ident = str(r.get("uuid") or r.get("url") or r.get("title") or "")
        if not ident:
            continue
        for e in r.get("entities") or []:
            if not isinstance(e, dict) or str(e.get("symbol") or "").upper() != symbol:
                continue
            s = e.get("sentiment_score")
            if isinstance(s, (int, float)) and not isinstance(s, bool):
                out[ident] = float(s)
                break
    return out


def summarize(scores: Dict[str, float]) -> dict:
    vals = list(scores.values())
    if not vals:
        return {"status": "no_data", "avg": None, "count": 0, "positive": 0, "negative": 0, "neutral": 0}
    return {
        "status": "ok",
        "avg": round(sum(vals) / len(vals), 4),
        "count": len(vals),
        "positive": sum(1 for v in vals if v > 0),
        "negative": sum(1 for v in vals if v < 0),
        "neutral": sum(1 for v in vals if v == 0),
    }


def collect(
    root: Path,
    *,
    now: dt.datetime,
    settings: Any = None,
    client: Optional[MarketauxClient] = None,
    symbols: Optional[List[str]] = None,
    dry_run: bool = False,
    max_pages: int = MAX_PAGES,
) -> dict:
    root = Path(root)
    today = now.astimezone(TAIPEI).date()
    after = published_after(today)
    syms = [s.strip().upper() for s in (symbols if symbols is not None else DEFAULT_SYMBOLS) if s.strip()]

    if dry_run:
        return {"status": "dry_run", "symbols": syms, "published_after": after,
                "planned_requests": len(syms) * max_pages,
                "sentiment_path": str(sentiment_path(root, today)),
                "news_path": str(news_path(root, today))}

    if client is None:
        if settings is None:
            from bot.config import Settings

            settings = Settings()
        key = str(getattr(settings, "marketaux_api_token", "") or "").strip()
        if not key:
            return {"status": "skipped"}
        client = MarketauxClient(
            key, quota=default_quota("marketaux", directory=root / "data" / "api_quota")
        )

    items: List[dict] = []
    table: Dict[str, dict] = {}
    errors: Dict[str, str] = {}
    requests_made = 0
    status = "ok"
    stop = False

    for sym in syms:
        if stop:
            break
        scores: Dict[str, float] = {}
        failed = False
        for page in range(1, max_pages + 1):
            try:
                requests_made += 1
                body = client.news_all(sym, after, page=page)
            except QuotaExceeded as exc:
                requests_made -= 1  # 未送出
                status, stop, failed = "quota_exceeded", True, True
                errors[sym] = str(exc)
                break
            except SourceError as exc:
                errors[sym] = f"{exc.kind}: {exc.message}"
                log.warning("marketaux %s failed: %s", sym, exc.kind)
                failed = True
                if exc.kind in ("quota_exhausted", "rate_limited"):
                    status = "quota_exceeded" if exc.kind == "quota_exhausted" else "rate_limited"
                    stop = True
                break
            items.extend(normalize_news(body))
            scores.update(_symbol_scores(body, sym))
            meta = body.get("meta") if isinstance(body, dict) else None
            meta = meta if isinstance(meta, dict) else {}
            returned = meta.get("returned")
            found = meta.get("found")
            if returned == 0 or (isinstance(found, int) and found <= page * PAGE_SIZE):
                break  # 沒有下一頁，不浪費額度
        if failed and not scores:
            table[sym] = {"status": "error", "avg": None, "count": 0, "positive": 0, "negative": 0,
                          "neutral": 0, "error": redact(errors.get(sym, ""))}
        else:
            table[sym] = summarize(scores)

    # 沒有任何結果就不動既有檔；出錯的代號保留既有檔中的舊資料
    s_path = sentiment_path(root, today)
    prev = read_json(s_path, {})
    prev_syms = prev.get("symbols") if isinstance(prev, dict) and isinstance(prev.get("symbols"), dict) else {}
    final = dict(prev_syms)
    for sym, row in table.items():
        if row["status"] == "error" and sym in prev_syms:
            continue
        final[sym] = row
    resolved = [s for s, r in table.items() if r["status"] != "error"]
    wrote_sent = False
    if resolved:
        atomic_json(s_path, {
            "date": today.isoformat(),
            "published_after": after,
            "source": "marketaux",
            "note": NOTE,
            "symbols": final,
        })
        wrote_sent = True

    n_path = news_path(root, today)
    existing = read_json(n_path, [])
    if not isinstance(existing, list):
        existing = []
    merged = merge_items(existing, items)
    wrote_news = False
    if items:
        atomic_json(n_path, merged)
        wrote_news = True

    if errors and status == "ok":
        status = "partial" if resolved else "error"
    return {
        "status": status,
        "symbols": table,
        "errors": {k: redact(v) for k, v in errors.items()},
        "requests": requests_made,
        "new_items": len(items),
        "total_items": len(merged),
        "published_after": after,
        "sentiment_path": str(s_path) if wrote_sent else None,
        "news_path": str(n_path) if wrote_news else None,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Marketaux 個股隔夜情緒收集")
    ap.add_argument("--dry-run", action="store_true", help="只列出將查詢的內容，不連網、不寫檔")
    ap.add_argument("--root", default=".", help="專案根目錄（預設目前目錄）")
    ap.add_argument("--symbols", help="逗號分隔的代號，預設 " + ",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--scheduled", action="store_true",
                    help="排程呼叫：額度用完或被限流時回傳 0，避免排程器自動重試而多花額度")
    args = ap.parse_args(argv)
    syms = [s for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    summary = collect(Path(args.root), now=dt.datetime.now(TAIPEI), symbols=syms, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    status = summary.get("status")
    if status in ("quota_exceeded", "rate_limited"):
        return 0 if args.scheduled else 1
    return 1 if status == "error" else 0


if __name__ == "__main__":
    sys.exit(main())

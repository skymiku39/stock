"""Massive 美股新聞收集：不帶 ticker，只帶時間範圍，寫 data/news/api_massive_<台灣日期>.json。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

from bot.free_sources.base import SourceError, iso_utc, redact
from bot.free_sources.massive import MassiveClient, normalize_news
from bot.free_sources.quota import TAIPEI, QuotaExceeded

NY = ZoneInfo("America/New_York")
MAX_PAGES = 3
PAGE_LIMIT = 1000  # 實測（2026-11-24）免費方案上限 1000，1001 回 400；週末窗口一頁 100 不夠


def news_path(root: Path, date: str) -> Path:
    return Path(root) / "data" / "news" / f"api_massive_{date}.json"


def window_start(now: dt.datetime) -> dt.datetime:
    """最近一個「已過」的平日美東 16:00（不處理假日）。"""
    ny = now.astimezone(NY)
    day = ny.date()
    while True:
        close = dt.datetime.combine(day, dt.time(16, 0), tzinfo=NY)
        if day.weekday() < 5 and close <= ny:
            return close
        day -= dt.timedelta(days=1)


def _key(item: dict) -> str:
    return item.get("url") or f"{item.get('title')}|{item.get('published_at')}"


def _load_existing(path: Path) -> List[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    return [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []


def _write(path: Path, items: List[dict]) -> None:
    from bot.runtime_state import atomic_json
    atomic_json(path, items)


def collect_massive(
    root: Path,
    settings: Any = None,
    *,
    session: Any = None,
    quota: Any = None,
    now: Optional[dt.datetime] = None,
    max_pages: int = MAX_PAGES,
    dry_run: bool = False,
) -> dict:
    if settings is None:
        from bot.config import Settings
        settings = Settings()
    key = (getattr(settings, "massive_api_key", "") or "").strip()
    now = now or dt.datetime.now(TAIPEI)
    date = now.astimezone(TAIPEI).date().isoformat()
    path = news_path(root, date)
    since = iso_utc(window_start(now))
    summary: dict = {"source": "massive", "date": date, "published_gte": since,
                     "max_pages": max_pages, "path": str(path)}
    if not key:
        return {**summary, "status": "skipped", "reason": "no api key"}
    if dry_run:
        return {**summary, "status": "dry_run", "params": {"limit": PAGE_LIMIT, "published_utc.gte": since}}

    client = MassiveClient(key, session=session, quota=quota)
    fetched: List[dict] = []
    pages = 0
    error: Optional[str] = None
    status = "ok"
    try:
        for page in client.iter_news(max_pages=max_pages, published_gte=since, limit=PAGE_LIMIT):
            pages += 1
            fetched.extend(normalize_news(page))
    except QuotaExceeded as exc:
        status, error = "quota", f"quota_exceeded:{exc.window}"
    except SourceError as exc:
        error = exc.kind
        status = "quota" if exc.kind in ("rate_limited", "quota_exhausted") else "error"
        summary["message"] = redact(exc.message, [key])
    summary.update(pages=pages, fetched=len(fetched), http_status=client.last_status)
    if error:
        summary["error"] = error
    if not fetched:
        return {**summary, "status": status, "new_items": 0}

    existing = _load_existing(path)
    seen = {_key(i) for i in existing}
    added = 0
    for item in fetched:
        k = _key(item)
        if k in seen:
            continue
        seen.add(k)
        existing.append(item)
        added += 1
    _write(path, existing)
    return {**summary, "status": status, "new_items": added, "total_items": len(existing)}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scheduled", action="store_true",
                        help="排程呼叫：額度用完或被限流時回傳 0，避免排程器自動重試而多花額度")
    args = parser.parse_args(argv)
    summary = collect_massive(args.root.resolve(), max_pages=args.max_pages, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False))
    status = summary["status"]
    if status == "quota" and args.scheduled:
        return 0
    return 0 if status in ("ok", "skipped", "dry_run") else 2


if __name__ == "__main__":
    raise SystemExit(main())

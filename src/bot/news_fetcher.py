"""news_fetcher -- 抓鉅亨網台股新聞 (含每日快取)。

設計
====
* 鉅亨網 API (https://api.cnyes.com) — 免註冊、回 JSON、最即時的台股新聞
* 每日快取於 `data/news/news_YYYY-MM-DD.json`
* 自動清理 HTML entities，輸出乾淨的 `title / summary / url / published_at`

用法
====
```python
from bot.news_fetcher import fetch_today_news
items = fetch_today_news(limit=200)         # 用快取
items = fetch_today_news(limit=200, force_refresh=True)
```
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.utils import get_logger, mk_folder, now_tw

CNYES_TW_STOCK = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock"
CNYES_TW_HEADLINE = "https://api.cnyes.com/media/api/v1/newslist/category/headline"

REQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------


@dataclass
class NewsItem:
    news_id: str
    title: str
    summary: str = ""
    url: str = ""
    source: str = "cnyes"
    category: str = ""
    published_at: str = ""        # ISO 8601
    keywords: list[str] = field(default_factory=list)
    related_tickers: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# 抓取
# ----------------------------------------------------------------------


_TICKER_RE = re.compile(r"\(([0-9]{4,6}[A-Z]?)-?T?W?\)")
_PURE_TICKER_RE = re.compile(r"\b([0-9]{4,6})\b")


def _clean_html(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_tickers(text: str) -> list[str]:
    """從新聞標題/摘要找出疑似 4-6 位數股票代號。

    優先抓 `(2330-TW)` 這種有明確 marker 的；再 fallback 全文 4-6 位數。
    """
    out: list[str] = []
    for m in _TICKER_RE.finditer(text or ""):
        t = m.group(1)
        if t not in out:
            out.append(t)
    if not out:
        for m in _PURE_TICKER_RE.finditer(text or ""):
            t = m.group(1)
            # 過濾年份 / 日期
            if t.startswith(("19", "20", "21")) and len(t) == 4:
                continue
            if t not in out:
                out.append(t)
    return out[:5]


def _parse_cnyes_item(d: dict[str, Any]) -> NewsItem | None:
    title = _clean_html(d.get("title") or "")
    if not title:
        return None
    summary = _clean_html(d.get("content") or d.get("summary") or "")
    if len(summary) > 300:
        summary = summary[:297] + "…"
    pub_ts = d.get("publishAt") or d.get("publish_at") or 0
    try:
        pub_iso = dt.datetime.fromtimestamp(int(pub_ts), tz=dt.UTC).astimezone().isoformat(timespec="seconds")
    except Exception:
        pub_iso = ""
    news_id = str(d.get("newsId") or d.get("id") or "")
    url = f"https://news.cnyes.com/news/id/{news_id}" if news_id else ""

    keywords_raw = d.get("keyword") or d.get("keywords") or []
    if isinstance(keywords_raw, str):
        keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    else:
        keywords = [str(k) for k in keywords_raw][:10]

    tickers = _extract_tickers(title + " " + summary)
    return NewsItem(
        news_id=news_id, title=title, summary=summary, url=url,
        source="cnyes", category=str(d.get("categoryName") or "台股"),
        published_at=pub_iso, keywords=keywords, related_tickers=tickers,
    )


def fetch_cnyes(
    *,
    limit: int = 100,
    timeout: int = 12,
    logger: logging.Logger | None = None,
) -> list[NewsItem]:
    log = logger or get_logger("news")
    out: list[NewsItem] = []
    per_page = min(50, limit)
    pages = (limit + per_page - 1) // per_page
    for p in range(1, pages + 1):
        try:
            resp = requests.get(
                CNYES_TW_STOCK,
                params={"limit": per_page, "page": p},
                headers=REQ_HEADERS, timeout=timeout,
            )
            if resp.status_code != 200:
                log.warning("cnyes p%d HTTP %d", p, resp.status_code)
                break
            data = resp.json().get("items", {}).get("data") or []
        except Exception:
            log.exception("cnyes p%d 失敗", p)
            break
        for d in data:
            item = _parse_cnyes_item(d)
            if item is None:
                continue
            out.append(item)
            if len(out) >= limit:
                break
        time.sleep(0.3)
        if len(out) >= limit:
            break
    log.info("cnyes 抓到 %d 條新聞", len(out))
    return out


# ----------------------------------------------------------------------
# 快取
# ----------------------------------------------------------------------


def _cache_path(date: dt.date, root: Path | None) -> Path:
    return (root or Path.cwd()) / "data" / "news" / f"news_{date.isoformat()}.json"


def _load_cache(date: dt.date, root: Path | None) -> list[NewsItem] | None:
    p = _cache_path(date, root)
    restore_file_from_cloud(p, root=root)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return [NewsItem(**d) for d in raw.get("items", []) or []]
    except Exception:
        return None


def _save_cache(items: list[NewsItem], date: dt.date, root: Path | None) -> Path:
    p = _cache_path(date, root)
    mk_folder(str(p.parent))
    p.write_text(json.dumps(
        {"asof": date.isoformat(), "items": [asdict(i) for i in items]},
        ensure_ascii=False, indent=2,
    ), encoding="utf-8")
    mirror_file_to_cloud(p, root=root)
    return p


def fetch_today_news(
    *,
    limit: int = 150,
    use_cache: bool = True,
    force_refresh: bool = False,
    root: Path | None = None,
    logger: logging.Logger | None = None,
) -> list[NewsItem]:
    """主要入口：抓今天的台股新聞 (有日快取)。"""
    log = logger or get_logger("news")
    today = now_tw().date()
    if use_cache and not force_refresh:
        cached = _load_cache(today, root)
        if cached:
            log.info("news 命中快取 %s (%d 條)", today, len(cached))
            return cached[:limit]
    items = fetch_cnyes(limit=limit, logger=log)
    if items:
        try:
            _save_cache(items, today, root)
        except Exception:
            log.exception("news 快取寫入失敗")
    return items


def news_to_compact_text(items: list[NewsItem], max_chars: int = 8000) -> str:
    """壓成緊湊文字，給 LLM 當 prompt input。"""
    lines: list[str] = []
    total = 0
    for i, it in enumerate(items, 1):
        line = f"{i:03d}. [{it.category}] {it.title}"
        if it.related_tickers:
            line += f" ({','.join(it.related_tickers)})"
        if it.summary and len(line) < 200:
            line += f" — {it.summary[:120]}"
        lines.append(line)
        total += len(line) + 1
        if total > max_chars:
            lines.append("…(以下省略)")
            break
    return "\n".join(lines)


__all__ = [
    "NewsItem",
    "fetch_cnyes",
    "fetch_today_news",
    "news_to_compact_text",
]

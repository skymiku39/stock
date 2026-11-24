"""每日 RSS 存檔：把 `web_search.RSS_FEEDS` 的項目累積成當日清單，與模型研究無關。

為什麼需要：RSS 原本只在個股研究（需要模型）時才下載，模型被擋的期間完全不會留存。
這裡逐一下載（沿用 `web_search` 的硬性時限、5MB 上限、30 分鐘快取與 raw 保存），
把看過的項目依網址去重後寫進 ``data/news/rss_<date>.json``。

受時間預算限制：一次沒跑完的來源，下次從中斷處接續（游標存在輸出檔）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from bot.runtime_state import atomic_json, read_json
from bot.utils import get_logger, now_tw

BUDGET_SECONDS = 150
ITEM_MAX_AGE_DAYS = 3          # 太舊的項目不進當日清單（Google News 類來源會混入舊文）
DESC_MAX_CHARS = 500
DAY_MAX_ITEMS = 20000


def archive_path(root: Path, date: str) -> Path:
    return Path(root) / "data" / "news" / f"rss_{date}.json"


def _published_iso(value: str) -> str:
    from bot.web_search import _published_epoch
    epoch = _published_epoch(value)
    if epoch <= 0:
        return ""
    return dt.datetime.fromtimestamp(epoch, now_tw().tzinfo).isoformat(timespec="seconds")


def build_tagger(root: Path, symbols: Iterable[str]):
    """回傳 tag(lang, text) → 提到的追蹤標的代號。只讀本機的公司名冊，不連網。

    中文來源比對公司簡稱，或「(2330)」「2330.TW」這類明確寫出代號的寫法；單獨的數字不算，
    因為新聞裡的金額、年份很容易和代號撞號。英文來源用英文簡稱做字詞邊界比對。
    名稱比對會有少量誤判（簡稱剛好是常見詞），這個標記只用來找新聞，不當成事實依據。
    """
    import re
    import sqlite3
    from bot.company_info import english_name_for
    from bot.web_search import _en_matcher

    symbols = sorted({str(s).strip() for s in symbols if str(s).strip()})
    names: Dict[str, str] = {}
    db_path = Path(root) / "data" / "stock.db"
    if symbols and db_path.exists():
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            marks = ",".join("?" for _ in symbols)
            names = {row[0]: (row[1] or "").strip() for row in con.execute(
                f"SELECT symbol, short_name FROM stock_info WHERE symbol IN ({marks})", symbols)}
        except sqlite3.Error:
            names = {}
        finally:
            con.close()
    zh, en = [], []
    for symbol in symbols:
        code = re.compile(rf"[（(\[]\s*{re.escape(symbol)}(?:[-.](?:TW|TWO))?\s*[)）\]]|(?<![0-9A-Za-z]){re.escape(symbol)}\.TWO?\b")
        name = names.get(symbol, "")
        zh.append((symbol, name if len(name) >= 2 else "", code))
        english = english_name_for(symbol, Path(root))
        en.append((symbol, _en_matcher(english) if len(english) >= 3 else None, code))

    def tag(lang: str, text: str) -> List[str]:
        if lang == "en":
            return [s for s, matcher, code in en if (matcher and matcher.search(text)) or code.search(text)]
        return [s for s, name, code in zh if (name and name in text) or code.search(text)]

    return tag


def _top_tickers(items: List[Dict[str, Any]], limit: int = 10) -> List[List[Any]]:
    counts: Dict[str, int] = {}
    for item in items:
        for ticker in item.get("related_tickers") or []:
            counts[ticker] = counts.get(ticker, 0) + 1
    return [[t, n] for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def collect_rss_archive(
    root: Path,
    *,
    feeds: Optional[Iterable[Any]] = None,
    budget: float = BUDGET_SECONDS,
    session: Any = None,
    loader: Any = None,
    tracked: Iterable[str] = (),
) -> Dict[str, Any]:
    """下載尚未輪到的來源並併入當日清單；回傳摘要（不含項目本身）。

    tracked：要在項目上標記 related_tickers 的代號（追蹤標的）；不給就不標。
    """
    from bot import web_search

    log = get_logger("rss-archive")
    feeds = list(web_search.RSS_FEEDS if feeds is None else feeds)
    loader = loader or web_search._load_feed
    now = now_tw()
    date = now.date().isoformat()
    path = archive_path(root, date)
    previous = read_json(path)
    if previous.get("date") != date:
        previous = {}
    items: Dict[str, Dict[str, Any]] = {i["url"]: i for i in previous.get("items", []) if i.get("url")}
    feed_status: Dict[str, str] = dict(previous.get("feeds", {}))
    cursor = int(previous.get("cursor", 0)) % max(1, len(feeds))
    cutoff = now.timestamp() - ITEM_MAX_AGE_DAYS * 86400
    try:
        tag = build_tagger(root, tracked) if tracked else None
    except Exception:
        tag = None
    started = time.monotonic()
    visited = added = 0
    sess = session or web_search._new_session()
    for offset in range(len(feeds)):
        if time.monotonic() - started >= budget:
            break
        feed = feeds[(cursor + offset) % len(feeds)]
        visited += 1
        content, raw_id, state = loader(feed, session=sess, root=root, log=log)
        feed_status[feed.name] = state
        if content is None:
            continue
        try:
            parsed = web_search.parse_feed_items(content)
        except Exception as exc:  # 內容不是合法 XML
            feed_status[feed.name] = f"unparseable: {type(exc).__name__}"
            continue
        for row in parsed:
            url, title = row.get("url", "").strip(), row.get("title", "").strip()
            if not url or not title or url in items:
                continue
            published = _published_iso(row.get("published", ""))
            if published and dt.datetime.fromisoformat(published).timestamp() < cutoff:
                continue
            items[url] = {
                "title": title, "url": url, "desc": row.get("desc", "")[:DESC_MAX_CHARS],
                "published_at": published, "feed": feed.name, "region": feed.region,
                "lang": feed.lang, "stance": feed.stance,
                "first_seen_at": now.isoformat(timespec="seconds"), "source_document_id": raw_id,
                "related_tickers": tag(feed.lang, f"{title} {row.get('desc', '')}") if tag else [],
            }
            added += 1
    cursor = (cursor + visited) % max(1, len(feeds))
    ordered = sorted(items.values(), key=lambda i: (i.get("published_at") or i.get("first_seen_at") or ""),
                     reverse=True)[:DAY_MAX_ITEMS]
    known = {f.name for f in feeds}
    feed_status = {k: v for k, v in feed_status.items() if k in known}
    failed = sorted(k for k, v in feed_status.items() if v != "ok")
    summary = {
        "status": "ok" if ordered else "failed", "date": date,
        "collected_at": now_tw().isoformat(timespec="seconds"),
        "feeds_total": len(feeds), "feeds_seen": len(feed_status), "feeds_visited": visited,
        "feeds_ok": len(feed_status) - len(failed), "feeds_failed": failed,
        "new_items": added, "total_items": len(ordered),
        "today_items": sum(1 for i in ordered if (i.get("published_at") or "").startswith(date)),
        "tagged_items": sum(1 for i in ordered if i.get("related_tickers")),
        "top_tickers": _top_tickers(ordered),
        "path": str(path),
    }
    atomic_json(path, {**{k: v for k, v in summary.items() if k != "path"},
                       "cursor": cursor, "feeds": feed_status, "items": ordered})
    log.info("RSS 存檔 %s：來源 %d/%d 正常，新增 %d 則，累積 %d 則",
             date, summary["feeds_ok"], len(feeds), added, len(ordered))
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--budget", type=float, default=BUDGET_SECONDS, help="本次最多執行秒數")
    args = parser.parse_args(argv)
    summary = collect_rss_archive(args.root.resolve(), budget=args.budget)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())

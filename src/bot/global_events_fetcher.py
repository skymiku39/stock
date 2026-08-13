"""global_events_fetcher -- 全球科技事件多來源抓取。

來源
====
* 內建種子清單 (科技巨頭發表會 + 國際主展，依 supply_chain 自動映射台股)
* 科技巨頭官方活動頁 (Apple / Microsoft / Google / Samsung / Meta / AWS / NVIDIA)
* Apple Newsroom RSS
* Google News 關鍵字偵測 (補漏近期發表)

輸出統一 row dict，由 ``global_event_calendar`` 寫入快取。
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests

from bot.market_macro import load_supply_chain
from bot.utils import get_logger, now_tw

APPLE_NEWSROOM_RSS = "https://www.apple.com/newsroom/rss-feed.rss"

# 科技巨頭官方活動頁 (輕量 HTML 日期解析)
CORPORATE_EVENT_PAGES: tuple[dict[str, str], ...] = (
    {"url": "https://developer.apple.com/wwdc/", "organizer": "AAPL", "hint": "wwdc", "title": "WWDC Keynote"},
    {"url": "https://www.apple.com/apple-events/", "organizer": "AAPL", "hint": "apple-event", "title": "Apple Event"},
    {"url": "https://build.microsoft.com/en-US/home", "organizer": "MSFT", "hint": "microsoft-build", "title": "Microsoft Build"},
    {"url": "https://io.google/", "organizer": "GOOGL", "hint": "google-io", "title": "Google I/O"},
    {"url": "https://www.samsung.com/global/galaxy/unpacked/", "organizer": "SAMSUNG", "hint": "samsung-unpacked", "title": "Samsung Galaxy Unpacked"},
    {"url": "https://about.meta.com/meta-connect/", "organizer": "META", "hint": "meta-connect", "title": "Meta Connect"},
    {"url": "https://reinvent.awsevents.com/", "organizer": "AMZN", "hint": "aws-reinvent", "title": "AWS re:Invent"},
    {"url": "https://www.nvidia.com/gtc/", "organizer": "NVDA", "hint": "nvidia-gtc", "title": "NVIDIA GTC"},
)

# 新聞關鍵字查詢 (依當年動態替換)
NEWS_QUERY_TEMPLATES = (
    "WWDC {year} Apple",
    "Apple Event keynote {year}",
    "Microsoft Build {year}",
    "Google I/O {year}",
    "Samsung Galaxy Unpacked {year}",
    "Meta Connect {year}",
    "AWS re:Invent {year}",
    "NVIDIA GTC {year}",
    "CES {year} technology",
    "MWC Barcelona {year}",
)

NEWS_KEYWORD_RE = re.compile(
    r"(WWDC|Apple\s+Event|Siri\s+AI|iOS\s+\d+|iPadOS\s+\d+|macOS\s+\d+|"
    r"Microsoft\s+Build|Google\s+I/?O|Galaxy\s+Unpacked|Samsung\s+Unpacked|"
    r"Meta\s+Connect|AWS\s+re:?Invent|re:Invent|"
    r"NVIDIA\s+GTC|GTC\s+\d{4}|CES\s+\d{4}|MWC\s+\d{4}|"
    r"Azure\s+AI|Copilot)",
    re.IGNORECASE,
)

ORGANIZER_FROM_TITLE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"apple|wwdc|siri|ios|ipados|macos|visionos", re.IGNORECASE), "AAPL"),
    (re.compile(r"nvidia|gtc", re.IGNORECASE), "NVDA"),
    (re.compile(r"google\s+i/?o|alphabet", re.IGNORECASE), "GOOGL"),
    (re.compile(r"microsoft\s+build|azure|copilot", re.IGNORECASE), "MSFT"),
    (re.compile(r"meta\s+connect", re.IGNORECASE), "META"),
    (re.compile(r"samsung|galaxy\s+unpacked", re.IGNORECASE), "SAMSUNG"),
    (re.compile(r"aws\s+re:?invent|re:invent", re.IGNORECASE), "AMZN"),
    (re.compile(r"amd\s+advancing", re.IGNORECASE), "AMD"),
)

# supply_chain 中視為「必須有行事曆覆蓋」的巨頭代號
MEGA_TECH_ORGANIZERS = frozenset({
    "AAPL", "MSFT", "GOOGL", "META", "AMZN", "NVDA", "AMD", "SAMSUNG",
})

DEFAULT_MIN_SUPPLY_WEIGHT = 0.5
NEWS_LOOKBACK_HOURS = 48
MAX_MERGED_NOTE_CHARS = 280


@dataclass(frozen=True)
class GlobalTechSeed:
    date: str
    end_date: str
    title: str
    event_type: str
    organizer: str
    location: str
    time: str
    note: str
    source: str
    url: str
    canonical_key: str
    source_quality: str = "official"
    verified_at: str = ""
    enabled: bool = True
    tickers: tuple[str, ...] = ()


def _news_queries_for_year(year: int) -> tuple[str, ...]:
    return tuple(t.format(year=year) for t in NEWS_QUERY_TEMPLATES)


def _mega_tech_seeds_for_year(year: int) -> list[GlobalTechSeed]:
    """年度科技巨頭 + 國際主展種子 (日期未確認者標 estimated)。"""
    y = year
    return [
        GlobalTechSeed(
            date=f"{y}-01-07", end_date=f"{y}-01-10",
            title=f"CES {y}", event_type="exhibition", organizer="",
            location="Las Vegas, USA", time="",
            note="消費電子與 AI 硬體大展。",
            source="內建種子", url="https://www.ces.tech/",
            canonical_key=f"ces-{y}",
            tickers=("2330", "2317", "2382", "2454", "3231", "2408"),
        ),
        GlobalTechSeed(
            date=f"{y}-01-22", end_date=f"{y}-01-22",
            title=f"Samsung Galaxy Unpacked {y} (春季)",
            event_type="keynote", organizer="SAMSUNG",
            location="San Jose / Online", time="",
            note="三星 Galaxy S 系列春季發表會 (日期待官方確認，依歷史慣例預估)。",
            source="內建種子", url="https://www.samsung.com/global/galaxy/unpacked/",
            canonical_key=f"samsung-unpacked-spring-{y}",
            source_quality="estimated",
        ),
        GlobalTechSeed(
            date=f"{y}-03-02", end_date=f"{y}-03-05",
            title=f"MWC Barcelona {y}", event_type="exhibition", organizer="",
            location="Barcelona, Spain", time="",
            note="全球行動通訊大展。",
            source="內建種子", url="https://www.mwcbarcelona.com/",
            canonical_key=f"mwc-barcelona-{y}",
            tickers=("2454", "2330", "2317", "3037"),
        ),
        GlobalTechSeed(
            date=f"{y}-03-17", end_date=f"{y}-03-21",
            title=f"NVIDIA GTC {y}", event_type="developer_conference", organizer="NVDA",
            location="San Jose, USA", time="",
            note="NVIDIA 年度開發者大會。",
            source="內建種子", url="https://www.nvidia.com/gtc/",
            canonical_key=f"nvidia-gtc-{y}",
        ),
        GlobalTechSeed(
            date=f"{y}-05-19", end_date=f"{y}-05-20",
            title=f"Microsoft Build {y}", event_type="developer_conference", organizer="MSFT",
            location="Seattle / Online", time="",
            note="Microsoft 年度開發者大會：Azure、Copilot、Windows AI (日期待官方確認)。",
            source="內建種子", url="https://build.microsoft.com/",
            canonical_key=f"microsoft-build-{y}",
            source_quality="estimated",
        ),
        GlobalTechSeed(
            date=f"{y}-05-20", end_date=f"{y}-05-21",
            title=f"Google I/O {y}", event_type="developer_conference", organizer="GOOGL",
            location="Mountain View / Online", time="",
            note="Google 年度開發者大會：Android、Gemini、Cloud TPU (日期待官方確認)。",
            source="內建種子", url="https://io.google/",
            canonical_key=f"google-io-{y}",
            source_quality="estimated",
        ),
        GlobalTechSeed(
            date=f"{y}-06-08", end_date=f"{y}-06-12",
            title=f"WWDC {y} Keynote", event_type="keynote", organizer="AAPL",
            location="Apple Park, Cupertino", time="10:00 PT",
            note="Apple 全球開發者大會 Keynote。",
            source="內建種子", url="https://developer.apple.com/wwdc/",
            canonical_key=f"wwdc-{y}-keynote",
        ),
        GlobalTechSeed(
            date=f"{y}-07-09", end_date=f"{y}-07-09",
            title=f"Samsung Galaxy Unpacked {y} (夏季)",
            event_type="keynote", organizer="SAMSUNG",
            location="Seoul / Online", time="",
            note="三星折疊機 / 下半年旗艦發表 (日期待官方確認)。",
            source="內建種子", url="https://www.samsung.com/global/galaxy/unpacked/",
            canonical_key=f"samsung-unpacked-summer-{y}",
            source_quality="estimated",
        ),
        GlobalTechSeed(
            date=f"{y}-09-09", end_date=f"{y}-09-09",
            title=f"Apple Fall Event {y} (預估)", event_type="keynote", organizer="AAPL",
            location="Apple Park, Cupertino", time="",
            note="秋季 iPhone 發表會 (日期待官方確認)。",
            source="內建種子", url="https://www.apple.com/apple-events/",
            canonical_key=f"apple-fall-event-{y}",
            source_quality="estimated",
        ),
        GlobalTechSeed(
            date=f"{y}-09-17", end_date=f"{y}-09-17",
            title=f"Meta Connect {y}", event_type="keynote", organizer="META",
            location="Menlo Park / Online", time="",
            note="Meta VR/AR 與 AI 發表會 (日期待官方確認)。",
            source="內建種子", url="https://about.meta.com/meta-connect/",
            canonical_key=f"meta-connect-{y}",
            source_quality="estimated",
        ),
        GlobalTechSeed(
            date=f"{y}-10-10", end_date=f"{y}-10-10",
            title=f"AMD Advancing AI {y} (預估)", event_type="keynote", organizer="AMD",
            location="San Francisco / Online", time="",
            note="AMD AI / Instinct 產品發表 (日期待官方確認)。",
            source="內建種子", url="https://www.amd.com/",
            canonical_key=f"amd-advancing-ai-{y}",
            source_quality="estimated",
        ),
        GlobalTechSeed(
            date=f"{y}-11-30", end_date=f"{y}-12-04",
            title=f"AWS re:Invent {y}", event_type="developer_conference", organizer="AMZN",
            location="Las Vegas, USA", time="",
            note="AWS 年度雲端大會 (日期待官方確認)。",
            source="內建種子", url="https://reinvent.awsevents.com/",
            canonical_key=f"aws-reinvent-{y}",
            source_quality="estimated",
        ),
    ]


def _default_global_tech_seeds() -> list[GlobalTechSeed]:
    year = now_tw().year
    return _mega_tech_seeds_for_year(year)


def tickers_for_organizer(
    organizer: str,
    *,
    root: Path | None = None,
    min_weight: float = DEFAULT_MIN_SUPPLY_WEIGHT,
    explicit: Sequence[str] | None = None,
) -> list[str]:
    if explicit:
        return [str(t).strip() for t in explicit if str(t).strip()]
    if not organizer:
        return []
    sc = load_supply_chain(root)
    info = (sc.get("us_stocks") or {}).get(organizer) or {}
    out: list[str] = []
    for row in info.get("tw_supply_chain") or []:
        try:
            weight = float(row.get("weight") or 0)
        except (TypeError, ValueError):
            weight = 0.0
        ticker = str(row.get("tw_ticker") or "").strip()
        if ticker and weight >= min_weight and ticker not in out:
            out.append(ticker)
    return out


def mega_tech_organizers_in_supply_chain(*, root: Path | None = None) -> list[str]:
    sc = load_supply_chain(root)
    keys = set((sc.get("us_stocks") or {}).keys())
    return sorted(keys & MEGA_TECH_ORGANIZERS)


def _seed_to_row(seed: GlobalTechSeed, *, root: Path | None) -> dict[str, Any]:
    tickers = list(seed.tickers) or tickers_for_organizer(seed.organizer, root=root)
    row = asdict(seed)
    row["tickers"] = tickers
    if not row.get("verified_at"):
        row["verified_at"] = now_tw().date().isoformat()
    return row


def _parse_rss_pubdate(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except Exception:
        return None


def fetch_apple_newsroom_events(
    *,
    root: Path | None = None,
    session: requests.Session | None = None,
    lookback_hours: int = NEWS_LOOKBACK_HOURS,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """從 Apple Newsroom RSS 偵測近期發表。"""
    log = logger or get_logger("global-events")
    sess = session or requests.Session()
    sess.headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    )
    out: list[dict[str, Any]] = []
    try:
        resp = sess.get(APPLE_NEWSROOM_RSS, timeout=20)
        resp.raise_for_status()
        root_el = ET.fromstring(resp.content)
    except Exception as exc:  # noqa: BLE001
        log.warning("Apple Newsroom RSS 抓取失敗: %s", exc)
        return []

    cutoff = now_tw() - dt.timedelta(hours=lookback_hours)
    for item in root_el.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = _parse_rss_pubdate(item.findtext("pubDate") or "")
        if not title or not pub:
            continue
        pub_local = pub.astimezone(now_tw().tzinfo) if pub.tzinfo else pub.replace(tzinfo=now_tw().tzinfo)
        if pub_local < cutoff:
            continue
        if not NEWS_KEYWORD_RE.search(title):
            continue
        day = pub_local.date()
        canonical = canonical_from_title(title, day)
        out.append({
            "date": day.isoformat(),
            "end_date": day.isoformat(),
            "title": title[:120],
            "event_type": "keynote",
            "organizer": "AAPL",
            "location": "Apple Park",
            "time": pub_local.strftime("%H:%M"),
            "note": f"Apple Newsroom: {title[:120]}",
            "source": "apple_newsroom",
            "source_quality": "official",
            "url": link,
            "canonical_key": canonical,
            "verified_at": now_tw().date().isoformat(),
            "enabled": True,
            "tickers": tickers_for_organizer("AAPL", root=root),
        })
    return out


def canonical_from_title(title: str, day: dt.date) -> str:
    lower = title.lower()
    year = day.year
    if "wwdc" in lower:
        return f"wwdc-{year}-keynote"
    if "microsoft build" in lower or re.search(r"\bbuild\b", lower) and "microsoft" in lower:
        return f"microsoft-build-{year}"
    if "google i/o" in lower or "google io" in lower:
        return f"google-io-{year}"
    if "galaxy unpacked" in lower or "samsung unpacked" in lower:
        season = "summer" if day.month >= 6 else "spring"
        return f"samsung-unpacked-{season}-{year}"
    if "meta connect" in lower:
        return f"meta-connect-{year}"
    if "re:invent" in lower or "reinvent" in lower:
        return f"aws-reinvent-{year}"
    if "gtc" in lower and ("nvidia" in lower or "nvidia" not in lower):
        return f"nvidia-gtc-{year}"
    if "ces" in lower:
        return f"ces-{year}"
    if "mwc" in lower:
        return f"mwc-barcelona-{year}"
    if "siri" in lower:
        return f"apple-siri-{day.isoformat()}"
    slug = re.sub(r"[^a-z0-9]+", "-", lower).strip("-")[:40]
    org = _organizer_from_text(title) or "news"
    return f"{org.lower()}-news-{day.isoformat()}-{slug}"


def fetch_corporate_official_pages(
    *,
    root: Path | None = None,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """解析科技巨頭官方活動頁中的日期線索。"""
    log = logger or get_logger("global-events")
    sess = session or requests.Session()
    sess.headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    )
    out: list[dict[str, Any]] = []
    min_year = now_tw().year - 1
    for spec in CORPORATE_EVENT_PAGES:
        url = spec["url"]
        organizer = spec["organizer"]
        hint = spec["hint"]
        base_title = spec["title"]
        try:
            resp = sess.get(url, timeout=20)
            resp.raise_for_status()
            text = html.unescape(resp.text)
        except Exception as exc:  # noqa: BLE001
            log.debug("官方頁抓取失敗 %s: %s", url, exc)
            continue
        dates = _extract_iso_dates(text)
        if not dates:
            continue
        start = min(dates)
        end = max(dates)
        if start.year < min_year:
            continue
        # 活動頁常含多個無關日期；超過 45 天視為解析雜訊，改由種子保底
        if (end - start).days > 45:
            log.debug("官方頁日期範圍過寬，略過 %s (%s ~ %s)", url, start, end)
            continue
        canonical = f"{hint}-{start.year}"
        if hint == "wwdc":
            canonical = f"wwdc-{start.year}-keynote"
        out.append({
            "date": start.isoformat(),
            "end_date": end.isoformat(),
            "title": f"{base_title} {start.year}",
            "event_type": "keynote" if "keynote" in hint or hint in {"wwdc", "apple-event", "samsung-unpacked", "meta-connect"} else "developer_conference",
            "organizer": organizer,
            "location": "",
            "time": "",
            "note": f"自官方頁解析: {url}",
            "source": "corporate_official_page",
            "source_quality": "official",
            "url": url,
            "canonical_key": canonical,
            "verified_at": now_tw().date().isoformat(),
            "enabled": True,
            "tickers": tickers_for_organizer(organizer, root=root),
        })
    return out


def fetch_apple_official_pages(
    *,
    root: Path | None = None,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """向後相容：僅 Apple 官方頁。"""
    rows = fetch_corporate_official_pages(root=root, session=session, logger=logger)
    return [r for r in rows if r.get("organizer") == "AAPL"]


def _extract_iso_dates(text: str) -> list[dt.date]:
    out: list[dt.date] = []
    for m in re.finditer(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text):
        try:
            out.append(dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    for m in re.finditer(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{1,2}),?\s+(20\d{2})\b",
        text,
        re.IGNORECASE,
    ):
        try:
            out.append(dt.datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y",
            ).date())
        except ValueError:
            continue
    return out


def fetch_news_keyword_events(
    *,
    root: Path | None = None,
    session: requests.Session | None = None,
    lookback_hours: int = NEWS_LOOKBACK_HOURS,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """以 Google News 關鍵字補漏近期全球科技發表。"""
    from bot.web_search import search_news

    log = logger or get_logger("global-events")
    sess = session or requests.Session()
    cutoff = now_tw() - dt.timedelta(hours=lookback_hours)
    seen_titles: set[str] = set()
    out: list[dict[str, Any]] = []
    year = now_tw().year

    for query in _news_queries_for_year(year):
        try:
            results = search_news(query, max_results=6, session=sess, logger=log)
        except Exception:
            log.debug("新聞搜尋失敗 q=%r", query, exc_info=True)
            continue
        for r in results:
            title = (r.title or "").strip()
            if not title or title in seen_titles:
                continue
            if not NEWS_KEYWORD_RE.search(title):
                continue
            seen_titles.add(title)
            pub_day = _news_published_date(r.published_at, cutoff)
            if not pub_day:
                continue
            organizer = _organizer_from_text(title)
            canonical = canonical_from_title(title, pub_day)
            out.append({
                "date": pub_day.isoformat(),
                "end_date": pub_day.isoformat(),
                "title": title[:120],
                "event_type": "keynote",
                "organizer": organizer,
                "location": "",
                "time": "",
                "note": (r.snippet or "")[:200],
                "source": "news_keyword",
                "source_quality": "news",
                "url": r.url,
                "canonical_key": canonical,
                "verified_at": now_tw().date().isoformat(),
                "enabled": True,
                "tickers": tickers_for_organizer(organizer, root=root) if organizer else [],
            })
    return out


def _news_published_date(published_at: str, cutoff: dt.datetime) -> dt.date | None:
    if not published_at:
        return now_tw().date()
    try:
        pub = dt.datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=now_tw().tzinfo)
        if pub < cutoff:
            return None
        return pub.date()
    except Exception:
        return now_tw().date()


def _organizer_from_text(text: str) -> str:
    for pattern, code in ORGANIZER_FROM_TITLE:
        if pattern.search(text):
            return code
    return ""


def merge_global_event_rows(
    *sources: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """依 canonical_key 合併；高品質來源優先，低品質不污染官方種子 note。"""
    quality_rank = {"official": 3, "estimated": 2, "news": 1}
    merged: dict[str, dict[str, Any]] = {}
    for rows in sources:
        for row in rows:
            if row.get("enabled") is False:
                continue
            key = str(row.get("canonical_key") or "").strip().lower()
            if not key:
                title = str(row.get("title") or "")
                day = str(row.get("date") or "")
                key = re.sub(r"[^a-z0-9]+", "-", f"{title}-{day}".lower()).strip("-")
                row = dict(row)
                row["canonical_key"] = key
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(row)
                continue
            new_q = quality_rank.get(str(row.get("source_quality") or ""), 0)
            old_q = quality_rank.get(str(existing.get("source_quality") or ""), 0)
            if new_q > old_q:
                note_extra = existing.get("note") or ""
                merged[key] = dict(row)
                if note_extra and note_extra not in (merged[key].get("note") or ""):
                    merged[key]["note"] = f"{merged[key].get('note', '')} | {note_extra}".strip(" |")
            else:
                if row.get("verified_at"):
                    existing["verified_at"] = row["verified_at"]
                new_note = row.get("note") or ""
                if new_q < old_q and old_q >= quality_rank["estimated"]:
                    pass
                elif new_note and new_note not in (existing.get("note") or ""):
                    combined = f"{existing.get('note', '')} | {new_note}".strip(" |")
                    existing["note"] = combined[:MAX_MERGED_NOTE_CHARS]
                if row.get("url") and not existing.get("url"):
                    existing["url"] = row["url"]
    return sorted(
        merged.values(),
        key=lambda r: (str(r.get("date") or ""), str(r.get("title") or "")),
    )


def fetch_all_global_events(
    *,
    root: Path | None = None,
    session: requests.Session | None = None,
    include_network: bool = True,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """收集所有來源並合併。"""
    log = logger or get_logger("global-events")
    seeds = [_seed_to_row(s, root=root) for s in _default_global_tech_seeds()]
    if not include_network:
        return merge_global_event_rows(seeds)

    sess = session or requests.Session()
    page_rows = fetch_corporate_official_pages(root=root, session=sess, logger=log)
    rss_rows = fetch_apple_newsroom_events(root=root, session=sess, logger=log)
    news_rows = fetch_news_keyword_events(root=root, session=sess, logger=log)
    merged = merge_global_event_rows(seeds, page_rows, rss_rows, news_rows)

    covered = {str(r.get("organizer") or "") for r in merged if r.get("organizer")}
    missing = sorted(MEGA_TECH_ORGANIZERS - covered)
    if missing:
        log.warning("全球事件種子未覆蓋巨頭: %s (請檢查種子或官方頁解析)", missing)

    log.info(
        "全球科技事件合併完成: seeds=%d pages=%d rss=%d news=%d total=%d organizers=%s",
        len(seeds), len(page_rows), len(rss_rows), len(news_rows), len(merged),
        sorted(covered & MEGA_TECH_ORGANIZERS),
    )
    return merged


# 向後相容別名
_canonical_from_title = canonical_from_title

__all__ = [
    "MEGA_TECH_ORGANIZERS",
    "GlobalTechSeed",
    "canonical_from_title",
    "fetch_all_global_events",
    "fetch_apple_newsroom_events",
    "fetch_corporate_official_pages",
    "fetch_news_keyword_events",
    "mega_tech_organizers_in_supply_chain",
    "merge_global_event_rows",
    "tickers_for_organizer",
]

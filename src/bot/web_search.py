"""web_search -- 免註冊網頁搜尋，給 LLM 法說/個股研究自動補料。

設計
====
所有功能都是「自動化、免 API key、結果可快取」：

* DuckDuckGo HTML lite (https://html.duckduckgo.com/html) — 一般網頁搜尋
* Google News RSS (https://news.google.com/rss/search) — 中文新聞
* 簡易抓網頁主要文字 (`requests` + BeautifulSoup)
* 每日快取於 ``data/web_search/<key>_<YYYY-MM-DD>.json``，避免重複打外網

公開介面
========
* ``search_web(query, ...)`` -- 走 DuckDuckGo HTML
* ``search_news(query, ...)`` -- 走 Google News RSS
* ``fetch_page_text(url, ...)`` -- 抓單一網頁主要純文字
* ``research_ticker(ticker, name_hint, ...)`` -- 把 ticker 名字組成多個搜尋 query
  逐一搜尋並抓取部分原文，回傳整理好的 ``WebMaterial``

注意：本模組刻意輕量，不做反爬蟲規避；遇到 403/timeout 會 graceful return []。
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
from urllib.parse import quote_plus, urlparse

import requests

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.utils import get_logger, mk_folder, now_tw

try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAS_BS4 = True
except Exception:
    _HAS_BS4 = False


CACHE_DIR_REL = "data/web_search"
DEFAULT_AGE_HOURS = 12
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------


@dataclass
class SearchResult:
    """一筆搜尋結果。"""

    title: str
    url: str
    snippet: str = ""
    source: str = ""           # ddg / google_news
    site: str = ""             # 例 "udn.com"
    published_at: str = ""     # ISO；只有新聞才有


@dataclass
class PageContent:
    """抓取的單一網頁文字。"""

    url: str
    title: str = ""
    text: str = ""
    fetched_at: str = ""
    error: str = ""


@dataclass
class WebMaterial:
    """組合好的研究素材。"""

    ticker: str
    name: str = ""
    queries: list[str] = field(default_factory=list)
    search_results: list[SearchResult] = field(default_factory=list)
    pages: list[PageContent] = field(default_factory=list)
    fetched_at: str = ""

    def compact_text(self, max_chars: int = 10000) -> str:
        """壓成緊湊文字餵 LLM。"""
        parts: list[str] = []
        if self.search_results:
            parts.append("【網頁搜尋摘要】")
            for r in self.search_results[:25]:
                line = f"- ({r.site or r.source}) {r.title}"
                if r.snippet:
                    line += f" — {r.snippet[:180]}"
                parts.append(line)
        if self.pages:
            parts.append("\n【部分網頁原文】")
            for p in self.pages:
                if not p.text:
                    continue
                parts.append(f"\n## {p.title or p.url}")
                parts.append(f"來源: {p.url}")
                parts.append(p.text[:1500])
        joined = "\n".join(parts).strip()
        if len(joined) > max_chars:
            joined = joined[:max_chars] + "\n...(已截斷)"
        return joined


# ----------------------------------------------------------------------
# Session helper
# ----------------------------------------------------------------------


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    })
    return s


def _clean_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _site_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc
        return netloc.removeprefix("www.")
    except Exception:
        return ""


# ----------------------------------------------------------------------
# DuckDuckGo HTML 搜尋
# ----------------------------------------------------------------------

_DDG_HTML_URL = "https://html.duckduckgo.com/html/"


def search_web(
    query: str,
    *,
    max_results: int = 10,
    timeout: int = 12,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> list[SearchResult]:
    """走 DuckDuckGo HTML 端點抓搜尋結果。

    DDG 不需 API key、不限頻率；遇到失敗會回 []。
    """
    log = logger or get_logger("web-search")
    sess = session or _new_session()
    results: list[SearchResult] = []
    try:
        resp = sess.post(
            _DDG_HTML_URL,
            data={"q": query, "kl": "tw-tzh"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            log.warning("DDG HTTP %d for q=%r", resp.status_code, query)
            return []
        html_text = resp.text
    except Exception:
        log.exception("DDG 搜尋失敗 q=%r", query)
        return []

    if _HAS_BS4:
        soup = BeautifulSoup(html_text, "html.parser")
        for el in soup.select("div.result"):
            link_el = el.select_one("a.result__a")
            if link_el is None:
                continue
            title = link_el.get_text(" ", strip=True)
            href = link_el.get("href", "")
            snippet_el = el.select_one(".result__snippet")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            if not title or not href:
                continue
            results.append(SearchResult(
                title=title, url=href, snippet=snippet,
                source="ddg", site=_site_from_url(href),
            ))
            if len(results) >= max_results:
                break
    else:
        for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            html_text, re.DOTALL,
        ):
            href, title_html, snippet_html = m.group(1), m.group(2), m.group(3)
            title = _clean_text(title_html)
            snippet = _clean_text(snippet_html)
            if not title:
                continue
            results.append(SearchResult(
                title=title, url=href, snippet=snippet,
                source="ddg", site=_site_from_url(href),
            ))
            if len(results) >= max_results:
                break

    log.info("DDG q=%r -> %d 筆", query, len(results))
    return results


# ----------------------------------------------------------------------
# Google News RSS
# ----------------------------------------------------------------------

_GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
)


def search_news(
    query: str,
    *,
    max_results: int = 15,
    timeout: int = 12,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> list[SearchResult]:
    """走 Google News RSS 抓相關新聞。免 API key。"""
    log = logger or get_logger("web-search")
    sess = session or _new_session()
    url = _GOOGLE_NEWS_RSS.format(q=quote_plus(query))
    try:
        resp = sess.get(url, timeout=timeout)
        if resp.status_code != 200:
            log.warning("Google News HTTP %d for q=%r", resp.status_code, query)
            return []
        xml_text = resp.text
    except Exception:
        log.exception("Google News 抓取失敗 q=%r", query)
        return []

    results: list[SearchResult] = []
    items = re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL)
    for raw in items[: max_results * 2]:
        title = _extract_xml_tag(raw, "title")
        link = _extract_xml_tag(raw, "link")
        pub = _extract_xml_tag(raw, "pubDate")
        desc = _extract_xml_tag(raw, "description")
        source = _extract_xml_tag(raw, "source")
        title = _clean_text(title)
        if not title or not link:
            continue
        try:
            published_iso = (
                dt.datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")
                .isoformat(timespec="seconds")
                if pub else ""
            )
        except Exception:
            published_iso = pub
        results.append(SearchResult(
            title=title,
            url=link.strip(),
            snippet=_clean_text(desc),
            source="google_news",
            site=_clean_text(source) or _site_from_url(link),
            published_at=published_iso,
        ))
        if len(results) >= max_results:
            break
    log.info("Google News q=%r -> %d 筆", query, len(results))
    return results


def _extract_xml_tag(s: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", s, re.DOTALL | re.IGNORECASE)
    if not m:
        return ""
    val = m.group(1).strip()
    val = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", val, flags=re.DOTALL)
    return val


# ----------------------------------------------------------------------
# 抓取單一網頁主要文字
# ----------------------------------------------------------------------


def fetch_page_text(
    url: str,
    *,
    max_chars: int = 4000,
    timeout: int = 12,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> PageContent:
    """抓網頁的主要文字 (僅取 <article>/<main>/<body> 中明顯段落)。"""
    log = logger or get_logger("web-search")
    sess = session or _new_session()
    content = PageContent(url=url, fetched_at=now_tw().isoformat(timespec="seconds"))
    try:
        resp = sess.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            content.error = f"HTTP {resp.status_code}"
            return content
        resp.encoding = resp.apparent_encoding or "utf-8"
        body = resp.text
    except Exception as e:
        content.error = str(e)
        log.debug("fetch_page_text 失敗: %s (%s)", url, e)
        return content

    if _HAS_BS4:
        soup = BeautifulSoup(body, "html.parser")
        title_el = soup.find("title")
        content.title = title_el.get_text(strip=True) if title_el else ""
        for el in soup(["script", "style", "noscript", "nav", "footer", "aside"]):
            el.decompose()
        candidates = soup.find_all(["article", "main"])
        if not candidates:
            candidates = [soup.body or soup]
        chunks: list[str] = []
        for c in candidates:
            for p in c.find_all(["p", "li", "h1", "h2", "h3"]):
                text = p.get_text(" ", strip=True)
                if len(text) >= 20:
                    chunks.append(text)
                if sum(len(x) for x in chunks) >= max_chars:
                    break
            if sum(len(x) for x in chunks) >= max_chars:
                break
        full = "\n".join(chunks)[:max_chars]
        content.text = full
    else:
        title_m = re.search(r"<title[^>]*>(.*?)</title>", body, re.DOTALL | re.IGNORECASE)
        if title_m:
            content.title = _clean_text(title_m.group(1))
        text = re.sub(r"<script.*?</script>", "", body, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = _clean_text(text)
        content.text = text[:max_chars]
    return content


# ----------------------------------------------------------------------
# 個股自動研究 (組合)
# ----------------------------------------------------------------------


def _build_queries(ticker: str, name: str) -> list[str]:
    """組合多個搜尋 query：法說/重訊/籌碼/產業/題材。"""
    base = name or ticker
    return [
        f"{base} {ticker} 法說會",
        f"{base} {ticker} 法人說明會 重點",
        f"{base} {ticker} 財報",
        f"{base} {ticker} 重大訊息",
        f"{base} {ticker} 展望",
        f"{base} {ticker} 利多 OR 利空",
    ]


def _cache_path(key: str, root: Path | None) -> Path:
    today = now_tw().date().isoformat()
    return (root or Path.cwd()) / CACHE_DIR_REL / f"{key}_{today}.json"


def _load_cache(key: str, root: Path | None, max_age_hours: int) -> dict[str, Any] | None:
    p = _cache_path(key, root)
    restore_file_from_cloud(p, root=root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    ts = data.get("fetched_at")
    if not ts:
        return data
    try:
        fetched = dt.datetime.fromisoformat(ts.replace("Z", ""))
        if (now_tw().replace(tzinfo=None) - fetched).total_seconds() > max_age_hours * 3600:
            return None
    except Exception:
        pass
    return data


def _save_cache(key: str, root: Path | None, data: dict[str, Any]) -> Path:
    p = _cache_path(key, root)
    mk_folder(str(p.parent))
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    mirror_file_to_cloud(p, root=root)
    return p


def research_ticker(
    ticker: str,
    *,
    name: str = "",
    root: Path | None = None,
    max_pages: int = 5,
    max_results_per_query: int = 6,
    force_refresh: bool = False,
    max_age_hours: int = DEFAULT_AGE_HOURS,
    logger: logging.Logger | None = None,
) -> WebMaterial:
    """對 ``ticker`` 自動跑「DDG + Google News + 抓網頁原文」研究流程。

    Args:
        max_pages: 額外抓網頁原文的篇數上限 (DDG 結果優先)
        max_results_per_query: 每個 query 的搜尋結果上限
    """
    log = logger or get_logger("web-search")
    key = ticker
    if not force_refresh:
        cached = _load_cache(key, root, max_age_hours)
        if cached:
            try:
                material = WebMaterial(
                    ticker=cached.get("ticker", ticker),
                    name=cached.get("name", name),
                    queries=cached.get("queries", []) or [],
                    search_results=[SearchResult(**r) for r in cached.get("search_results", []) or []],
                    pages=[PageContent(**p) for p in cached.get("pages", []) or []],
                    fetched_at=cached.get("fetched_at", ""),
                )
                log.info("[%s] web research 命中快取", ticker)
                return material
            except Exception:
                log.debug("[%s] web research 快取格式異常，重抓", ticker)

    sess = _new_session()
    queries = _build_queries(ticker, name)
    all_results: list[SearchResult] = []
    seen_urls: set[str] = set()

    for q in queries:
        for r in search_news(q, max_results=max_results_per_query, session=sess, logger=log):
            if r.url in seen_urls:
                continue
            seen_urls.add(r.url)
            all_results.append(r)
        time.sleep(0.5)
        for r in search_web(q, max_results=max_results_per_query, session=sess, logger=log):
            if r.url in seen_urls:
                continue
            seen_urls.add(r.url)
            all_results.append(r)
        time.sleep(0.6)

    pages: list[PageContent] = []
    for r in all_results:
        if r.source != "ddg":
            continue
        if not r.url.startswith("http"):
            continue
        host = _site_from_url(r.url)
        if any(b in host for b in ("twitter.com", "facebook.com", "youtube.com", "tiktok.com")):
            continue
        page = fetch_page_text(r.url, session=sess, logger=log)
        if page.text:
            pages.append(page)
        if len(pages) >= max_pages:
            break
        time.sleep(0.4)

    material = WebMaterial(
        ticker=ticker, name=name,
        queries=queries,
        search_results=all_results,
        pages=pages,
        fetched_at=now_tw().isoformat(timespec="seconds"),
    )
    try:
        _save_cache(key, root, {
            "ticker": material.ticker,
            "name": material.name,
            "queries": material.queries,
            "fetched_at": material.fetched_at,
            "search_results": [asdict(r) for r in material.search_results],
            "pages": [asdict(p) for p in material.pages],
        })
    except Exception:
        log.exception("[%s] web research 快取寫入失敗", ticker)
    return material


__all__ = [
    "PageContent",
    "SearchResult",
    "WebMaterial",
    "fetch_page_text",
    "research_ticker",
    "search_news",
    "search_web",
]

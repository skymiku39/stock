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
import hashlib
import html
import collections
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, urlparse

import requests

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.source_registry import SourceRegistry
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
    source_document_id: str = ""


@dataclass
class PageContent:
    """抓取的單一網頁文字。"""

    url: str
    title: str = ""
    text: str = ""
    fetched_at: str = ""
    error: str = ""
    source_document_id: str = ""


@dataclass
class WebMaterial:
    """組合好的研究素材。"""

    ticker: str
    name: str = ""
    queries: List[str] = field(default_factory=list)
    search_results: List[SearchResult] = field(default_factory=list)
    pages: List[PageContent] = field(default_factory=list)
    fetched_at: str = ""
    source_status: Dict[str, str] = field(default_factory=dict)
    world_results: List[SearchResult] = field(default_factory=list)

    def compact_text(self, max_chars: int = 10000) -> str:
        """壓成緊湊文字餵 LLM。"""
        parts: List[str] = []
        unavailable = {
            k: v for k, v in (self.source_status or {}).items()
            if "unavailable" in str(v)
        }
        if unavailable:
            parts.append("【來源狀態】")
            for k, v in unavailable.items():
                label = _SOURCE_LABELS.get(k) or (
                    f"RSS {k[4:]}" if k.startswith("rss:") else k
                )
                reason = str(v).split("unavailable:", 1)[-1].strip() or str(v)
                parts.append(f"- {label}：不可用（{reason}），本次缺少該來源的搜尋結果")
            parts.append("")
        if self.search_results:
            parts.append("【網頁搜尋摘要】")
            for r in self.search_results[:25]:
                line = f"- ({r.site or r.source}) {r.title}"
                if r.snippet:
                    line += f" — {r.snippet[:180]}"
                parts.append(line)
        if self.world_results:
            parts.append("\n【國際與地緣政治】")
            for r in self.world_results[:15]:
                line = f"- ({r.site or r.source}) {r.title}"
                if r.snippet:
                    line += f" — {r.snippet[:160]}"
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


_SOURCE_LABELS = {
    "ddg": "DuckDuckGo 網頁搜尋",
    "google_news": "Google News",
    "world": "國際與地緣政治",
}


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
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def _materialize_search_results(
    root: Optional[Path],
    source_document_id: str,
    results: List[SearchResult],
    logger: logging.Logger,
) -> None:
    """把已保存的搜尋 raw response 接到 research/evidence/claim layer。"""
    if root is None or not source_document_id or not results:
        return
    try:
        from bot.web_search_research import materialize_web_search_slice

        result = materialize_web_search_slice(
            root=Path(root),
            source_document_id=source_document_id,
            items=results,
        )
        if result.status == "failed":
            logger.warning(
                "搜尋 research slice 失敗 source=%s: %s",
                source_document_id,
                result.error,
            )
    except Exception:
        # 搜尋 raw 與 adapter 結果仍可用；slice 失敗必須可由 audit 發現，
        # 但不能把一次 research 寫入失敗偽裝成「沒有搜尋結果」。
        logger.exception("搜尋 research slice 寫入失敗 source=%s", source_document_id)


# ----------------------------------------------------------------------
# DuckDuckGo HTML 搜尋
# ----------------------------------------------------------------------

_DDG_HTML_URL = "https://html.duckduckgo.com/html/"


class SearchUnavailable(RuntimeError):
    """The provider failed; this is distinct from a successful empty search."""


def _ddg_destination(href: str) -> str:
    href = html.unescape(str(href))
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.hostname in {"duckduckgo.com", "html.duckduckgo.com"} or href.startswith("/l/"):
        href = parse_qs(parsed.query).get("uddg", [href])[0]
    return href if urlparse(href).scheme in {"http", "https"} else ""


def search_web(
    query: str,
    *,
    max_results: int = 10,
    timeout: int = 12,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    root: Optional[Path] = None,
    strict: bool = False,
) -> List[SearchResult]:
    """走 DuckDuckGo HTML 端點抓搜尋結果。

    DDG 不需 API key；strict=True 時將拒絕、驗證頁與網路錯誤回報呼叫端。
    """
    log = logger or get_logger("web-search")
    sess = session or _new_session()
    results: List[SearchResult] = []
    raw_document_id = ""
    try:
        resp = sess.post(
            _DDG_HTML_URL,
            data={"q": query, "kl": "tw-tzh"},
            timeout=timeout,
        )
        if any(marker in resp.text.lower() for marker in ("challenge-form", "anomaly.js", "anomaly-modal")):
            raise SearchUnavailable(f"DuckDuckGo 回傳驗證頁 (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise SearchUnavailable(f"DuckDuckGo HTTP {resp.status_code}")
        if root is not None and getattr(resp, "content", b""):
            try:
                raw_document = SourceRegistry(Path(root)).capture_document(
                    "duckduckgo_search",
                    resp.content,
                    source_url=_DDG_HTML_URL,
                    source_ref=query,
                    title=f"DuckDuckGo 搜尋: {query}",
                    kind="search_response",
                    suffix="html",
                    mime_type="text/html; charset=utf-8",
                    metadata={"query": query, "max_results": max_results},
                )
                raw_document_id = raw_document.document_id
            except Exception:
                log.debug("DuckDuckGo raw 保存失敗", exc_info=True)
        html_text = resp.text
    except Exception as exc:
        if strict:
            if isinstance(exc, SearchUnavailable):
                raise
            raise SearchUnavailable(f"DuckDuckGo {type(exc).__name__}: {exc}") from exc
        log.warning("DDG 搜尋不可用 q=%r: %s", query, exc)
        return []

    if _HAS_BS4:
        soup = BeautifulSoup(html_text, "html.parser")
        for el in soup.select("div.result"):
            link_el = el.select_one("a.result__a")
            if link_el is None:
                continue
            title = link_el.get_text(" ", strip=True)
            href = _ddg_destination(link_el.get("href", ""))
            snippet_el = el.select_one(".result__snippet")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            if not title or not href:
                continue
            results.append(SearchResult(
                title=title, url=href, snippet=snippet,
                source="ddg", site=_site_from_url(href), source_document_id=raw_document_id,
            ))
            if len(results) >= max_results:
                break
    else:
        for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            html_text, re.S,
        ):
            href, title_html, snippet_html = _ddg_destination(m.group(1)), m.group(2), m.group(3)
            title = _clean_text(title_html)
            snippet = _clean_text(snippet_html)
            if not title or not href:
                continue
            results.append(SearchResult(
                title=title, url=href, snippet=snippet,
                source="ddg", site=_site_from_url(href), source_document_id=raw_document_id,
            ))
            if len(results) >= max_results:
                break

    if strict and not results and "no-results" not in html_text.lower():
        raise SearchUnavailable("DuckDuckGo 回應沒有可辨識的結果或查無結果標記")
    _materialize_search_results(root, raw_document_id, results, log)
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
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    root: Optional[Path] = None,
) -> List[SearchResult]:
    """走 Google News RSS 抓相關新聞。免 API key。"""
    log = logger or get_logger("web-search")
    sess = session or _new_session()
    url = _GOOGLE_NEWS_RSS.format(q=quote_plus(query))
    raw_document_id = ""
    try:
        resp = sess.get(url, timeout=timeout)
        if resp.status_code != 200:
            log.warning("Google News HTTP %d for q=%r", resp.status_code, query)
            return []
        if root is not None and getattr(resp, "content", b""):
            try:
                raw_document = SourceRegistry(Path(root)).capture_document(
                    "google_news_rss",
                    resp.content,
                    source_url=url,
                    source_ref=query,
                    title=f"Google News RSS: {query}",
                    kind="search_response",
                    suffix="xml",
                    mime_type="application/rss+xml",
                    metadata={"query": query, "max_results": max_results},
                )
                raw_document_id = raw_document.document_id
            except Exception:
                log.debug("Google News raw 保存失敗", exc_info=True)
        xml_text = resp.text
    except Exception:
        log.exception("Google News 抓取失敗 q=%r", query)
        return []

    results: List[SearchResult] = []
    items = re.findall(r"<item>(.*?)</item>", xml_text, re.S)
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
            source_document_id=raw_document_id,
        ))
        if len(results) >= max_results:
            break
    _materialize_search_results(root, raw_document_id, results, log)
    log.info("Google News q=%r -> %d 筆", query, len(results))
    return results


def _extract_xml_tag(s: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", s, re.S | re.I)
    if not m:
        return ""
    val = m.group(1).strip()
    val = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", val, flags=re.S)
    return val


# ----------------------------------------------------------------------
# 財經 RSS 訂閱
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Feed:
    """一個 RSS/Atom 來源。region: tw/us/uk/eu/asia；lang: zh/en。"""

    name: str
    url: str
    region: str
    lang: str


def _feeds(region: str, lang: str, pairs: tuple[tuple[str, str], ...]) -> tuple[Feed, ...]:
    return tuple(Feed(n, u, region, lang) for n, u in pairs)


_CNBC = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id="

RSS_FEEDS: tuple[Feed, ...] = (
    *_feeds("tw", "zh", (
        ("yahoo_tw_stock", "https://tw.stock.yahoo.com/rss?category=tw-market"),
        ("yahoo_tw_finance", "https://tw.news.yahoo.com/rss/finance"),
        ("udn_money_stock", "https://money.udn.com/rssfeed/news/1001/5590?ch=money"),
        ("udn_money_industry", "https://money.udn.com/rssfeed/news/1001/5591?ch=money"),
        ("ltn_business", "https://news.ltn.com.tw/rss/business.xml"),
        ("ctee_stock", "https://www.ctee.com.tw/rss_web/livenews/stock"),
        ("moneydj", "https://www.moneydj.com/KMDJ/RssCenter.aspx?svc=NR&fno=1&arg=MB010000"),
        ("cna_finance", "https://feeds.feedburner.com/rsscna/finance"),
        ("technews", "https://technews.tw/feed/"),
        ("digitimes", "https://www.digitimes.com.tw/tech/rss/xml/xmlrss_10_0.xml"),
        ("cna_tech", "https://feeds.feedburner.com/rsscna/technology"),
        ("cna_intl", "https://feeds.feedburner.com/rsscna/intworld"),
        ("cna_politics", "https://feeds.feedburner.com/rsscna/politics"),
        ("cna_china", "https://feeds.feedburner.com/rsscna/mainland"),
        ("udn_money_global", "https://money.udn.com/rssfeed/news/1001/5588?ch=money"),
        ("ltn_world", "https://news.ltn.com.tw/rss/world.xml"),
        ("ltn_politics", "https://news.ltn.com.tw/rss/politics.xml"),
        ("ettoday_finance", "https://feeds.feedburner.com/ettoday/finance"),
        ("ithome", "https://www.ithome.com.tw/rss"),
        ("cw", "https://www.cw.com.tw/RSS/cw_content.xml"),
        ("yahoo_tw_intl", "https://tw.stock.yahoo.com/rss?category=intl-markets"),
        ("yahoo_tw_tech", "https://tw.stock.yahoo.com/rss?category=tech"),
    )),
    *_feeds("us", "en", (
        ("cnbc_top", _CNBC + "100003114"),
        ("cnbc_markets", _CNBC + "15839069"),
        ("cnbc_tech", _CNBC + "19854910"),
        ("cnbc_asia", _CNBC + "19832390"),
        ("cnbc_politics", _CNBC + "10000113"),
        ("marketwatch_top", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("marketwatch_pulse", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
        ("wsj_markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
        ("wsj_tech", "https://feeds.content.dowjones.io/public/rss/RSSWSJD"),
        ("wsj_world", "https://feeds.content.dowjones.io/public/rss/RSSWorldNews"),
        ("bloomberg_markets", "https://feeds.bloomberg.com/markets/news.rss"),
        ("bloomberg_tech", "https://feeds.bloomberg.com/technology/news.rss"),
        ("bloomberg_politics", "https://feeds.bloomberg.com/politics/news.rss"),
        ("nasdaq_markets", "https://www.nasdaq.com/feed/rssoutbound?category=Markets"),
        ("seekingalpha", "https://seekingalpha.com/market_currents.xml"),
        ("investing", "https://www.investing.com/rss/news.rss"),
        ("nyt_business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
        ("nyt_tech", "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml"),
        ("nyt_politics", "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml"),
        ("fortune", "https://fortune.com/feed/"),
        ("businessinsider", "https://feeds.businessinsider.com/custom/all"),
        ("politico", "https://rss.politico.com/politics-news.xml"),
        ("thehill", "https://thehill.com/news/feed/"),
        ("npr_business", "https://feeds.npr.org/1006/rss.xml"),
        ("fed_press", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("techcrunch", "https://techcrunch.com/feed/"),
        ("eetimes", "https://www.eetimes.com/feed/"),
        ("tomshardware", "https://www.tomshardware.com/feeds/all"),
    )),
    *_feeds("uk", "en", (
        ("bbc_business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("bbc_world", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("bbc_asia", "https://feeds.bbci.co.uk/news/world/asia/rss.xml"),
        ("guardian_business", "https://www.theguardian.com/uk/business/rss"),
        ("guardian_world", "https://www.theguardian.com/world/rss"),
        ("ft_markets", "https://www.ft.com/markets?format=rss"),
        ("ft_world", "https://www.ft.com/world?format=rss"),
        ("economist_finance", "https://www.economist.com/finance-and-economics/rss.xml"),
        ("economist_asia", "https://www.economist.com/asia/rss.xml"),
        ("sky_business", "https://feeds.skynews.com/feeds/rss/business.xml"),
        ("boe_news", "https://www.bankofengland.co.uk/rss/news"),
    )),
    *_feeds("eu", "en", (
        ("dw_business", "https://rss.dw.com/rdf/rss-en-bus"),
        ("dw_asia", "https://rss.dw.com/rdf/rss-en-asia"),
        ("euronews_business", "https://www.euronews.com/rss?level=vertical&name=business"),
        ("france24_business", "https://www.france24.com/en/business/rss"),
        ("politico_eu", "https://www.politico.eu/feed/"),
        ("ecb_press", "https://www.ecb.europa.eu/rss/press.html"),
    )),
    *_feeds("asia", "en", (
        ("nikkei_asia", "https://asia.nikkei.com/rss/feed/nar"),
        ("scmp_business", "https://www.scmp.com/rss/92/feed"),
        ("scmp_china", "https://www.scmp.com/rss/4/feed"),
    )),
)


def _local(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _child_text(el: "ET.Element", *names: str) -> str:
    for child in el:
        if _local(child.tag) in names:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def parse_feed_items(content: bytes) -> List[Dict[str, str]]:
    """解析 RSS <item> 與 Atom <entry>，回傳 title/url/desc/published。"""
    root_el = ET.fromstring(content)
    items: List[Dict[str, str]] = []
    for el in root_el.iter():
        kind = _local(el.tag)
        if kind not in ("item", "entry"):
            continue
        link = ""
        if kind == "entry":
            for child in el:
                if _local(child.tag) == "link" and child.get("href"):
                    if not link or child.get("rel", "alternate") == "alternate":
                        link = child.get("href", "")
        if not link:
            link = _child_text(el, "link", "guid")
        items.append({
            "title": _clean_text(_child_text(el, "title")),
            "url": link.strip(),
            "desc": _clean_text(_child_text(el, "description", "summary", "content", "encoded")),
            "published": _child_text(el, "pubdate", "updated", "published", "date"),
        })
    return items


# ---- 抓取：硬性時限 + 共用快取 -----------------------------------------

RSS_CACHE_REL = "data/cache/rss"
RSS_MAX_BYTES = 5 * 1024 * 1024
_RSS_MEM: Dict[tuple[str, str], tuple[float, bytes, str]] = {}


class FeedFetchError(RuntimeError):
    """RSS 抓取失敗（訊息即可讀原因，例如 ``HTTP 503``）。"""


def _feed_reason(exc: BaseException) -> str:
    if isinstance(exc, FeedFetchError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _feed_cache_paths(root: Path, name: str) -> tuple[Path, Path]:
    base = Path(root) / RSS_CACHE_REL
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return base / f"{safe}.xml", base / f"{safe}.json"


def _read_feed_cache(
    feed: Feed, root: Optional[Path],
) -> Optional[tuple[float, bytes, str]]:
    """讀快取（不論是否過期）；回傳 (fetched_at, content, raw_document_id)。"""
    if root is None:
        return _RSS_MEM.get((feed.name, feed.url))
    xml_p, meta_p = _feed_cache_paths(root, feed.name)
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        content = xml_p.read_bytes()
        if not content:
            return None
        return float(meta.get("fetched_at", 0)), content, str(meta.get("raw_document_id", ""))
    except Exception:
        return None


def _write_feed_cache(
    feed: Feed, root: Optional[Path], content: bytes, raw_id: str, fetched_at: float,
) -> None:
    if root is None:
        _RSS_MEM[(feed.name, feed.url)] = (fetched_at, content, raw_id)
        return
    xml_p, meta_p = _feed_cache_paths(root, feed.name)
    mk_folder(str(xml_p.parent))
    xml_p.write_bytes(content)
    meta_p.write_text(
        json.dumps({"fetched_at": fetched_at, "raw_document_id": raw_id, "url": feed.url},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def _fetch_feed(
    feed: Feed,
    *,
    session: Optional[requests.Session] = None,
    root: Optional[Path] = None,
    ttl_seconds: float = 1800,
    deadline: float = 20,
    log: Optional[logging.Logger] = None,
) -> tuple[bytes, str, bool]:
    """抓單一 feed，回傳 (content, raw_document_id, from_cache)。

    TTL 內直接讀快取（不發請求、不重複保存 raw）。真的下載時用 stream + 硬性
    總時限 ``deadline`` 與 5MB 上限，避免慢速回傳讓程式卡死；失敗丟出例外。
    """
    logger_ = log or get_logger("web-search")
    cached = _read_feed_cache(feed, root)
    if cached is not None and ttl_seconds > 0 and (time.time() - cached[0]) < ttl_seconds:
        return cached[1], cached[2], True

    sess = session or _new_session()
    started = time.monotonic()
    resp = sess.get(feed.url, timeout=(8, 8), stream=True)
    try:
        if resp.status_code != 200:
            raise FeedFetchError(f"HTTP {resp.status_code}")
        chunks: List[bytes] = []
        size = 0
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            chunks.append(chunk)
            size += len(chunk)
            if size > RSS_MAX_BYTES:
                raise FeedFetchError(f"response too large (>{RSS_MAX_BYTES} bytes)")
            if time.monotonic() - started > deadline:
                raise FeedFetchError(f"deadline exceeded ({deadline}s)")
        if time.monotonic() - started > deadline:
            raise FeedFetchError(f"deadline exceeded ({deadline}s)")
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    content = b"".join(chunks)
    if not content:
        raise FeedFetchError("empty response")

    raw_document_id = ""
    if root is not None:
        try:
            raw_document = SourceRegistry(Path(root)).capture_document(
                "rss_feed",
                content,
                source_url=feed.url,
                source_ref=feed.name,
                title=f"RSS: {feed.name}",
                kind="search_response",
                suffix="xml",
                mime_type="application/rss+xml",
                metadata={"feed": feed.name, "region": feed.region, "lang": feed.lang},
            )
            raw_document_id = raw_document.document_id
        except Exception:
            logger_.debug("RSS raw 保存失敗: %s", feed.name, exc_info=True)
    try:
        _write_feed_cache(feed, root, content, raw_document_id, time.time())
    except Exception:
        logger_.debug("RSS 快取寫入失敗: %s", feed.name, exc_info=True)
    return content, raw_document_id, False


def _load_feed(
    feed: Feed,
    *,
    session: Optional[requests.Session],
    root: Optional[Path],
    log: logging.Logger,
    ttl_seconds: float = 1800,
    deadline: float = 20,
    pace: bool = True,
) -> tuple[Optional[bytes], str, str]:
    """``_fetch_feed`` + 失敗退回過期快取。回傳 (content|None, raw_id, state)。

    state：``ok`` / ``stale cache (<原因>)`` / ``unavailable: <原因>``。
    真的發出請求後 sleep 0.3 秒；讀快取不 sleep。
    """
    try:
        content, raw_id, from_cache = _fetch_feed(
            feed, session=session, root=root, ttl_seconds=ttl_seconds,
            deadline=deadline, log=log,
        )
    except Exception as exc:  # noqa: BLE001
        reason = _feed_reason(exc)
        log.debug("RSS %s 失敗", feed.name, exc_info=True)
        if pace:
            time.sleep(0.3)
        stale = _read_feed_cache(feed, root)
        if stale is not None:
            return stale[1], stale[2], f"stale cache ({reason})"
        return None, "", f"unavailable: {reason}"
    if not from_cache and pace:
        time.sleep(0.3)
    return content, raw_id, "ok"


# ---- 比對 -------------------------------------------------------------


def _is_ascii_text(s: str) -> bool:
    return bool(s) and s.isascii()


def _en_matcher(term: str) -> "re.Pattern[str]":
    """字詞邊界比對；長度 <=4 的全大寫縮寫（TSMC、ASE）區分大小寫。"""
    flags = 0 if (len(term) <= 4 and term.isupper()) else re.IGNORECASE
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", flags)


def search_feeds(
    ticker: str,
    name: str = "",
    *,
    english_name: str = "",
    feeds: tuple[Feed, ...] = RSS_FEEDS,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    root: Optional[Path] = None,
    max_results: int = 20,
) -> tuple[List[SearchResult], Dict[str, str]]:
    """抓財經 RSS/Atom 訂閱，保留標題或摘要含代號/名稱的項目。

    zh feed 用代號或中文名稱子字串比對；en feed 用英文簡稱（及純 ASCII 的 name）
    字詞邊界比對，沒有英文名稱時 en feed 記 ``skipped (no english name)``。
    回傳 (results, status)；status 以 feed 名稱為 key，單一 feed 失敗不影響其他。
    """
    log = logger or get_logger("web-search")
    sess = session or _new_session()
    results: List[SearchResult] = []
    status: Dict[str, str] = {}
    zh_needles = [x for x in (str(ticker or "").strip(), str(name or "").strip()) if x]
    en_terms: List[str] = []
    for cand in (str(english_name or "").strip(), str(name or "").strip()):
        if cand and _is_ascii_text(cand) and cand not in en_terms:
            en_terms.append(cand)
    en_matchers = [_en_matcher(t) for t in en_terms]
    per_lang: Dict[str, int] = collections.defaultdict(int)
    for feed in feeds:
        if feed.lang == "en" and not en_matchers:
            status[feed.name] = "skipped (no english name)"
            continue
        content, raw_document_id, state = _load_feed(feed, session=sess, root=root, log=log)
        if content is None:
            status[feed.name] = state
            continue
        try:
            matched = 0
            for item in parse_feed_items(content):
                if not item["title"] or not item["url"]:
                    continue
                hay = f"{item['title']} {item['desc']}"
                if feed.lang == "en":
                    hit = any(m.search(hay) for m in en_matchers)
                else:
                    hit = any(n in hay for n in zh_needles)
                if not hit:
                    continue
                matched += 1
                # 中文與英文來源各自計上限，避免排在前面的中文來源佔滿名額。
                if per_lang[feed.lang] >= max_results:
                    continue
                per_lang[feed.lang] += 1
                results.append(SearchResult(
                    title=item["title"],
                    url=item["url"],
                    snippet=item["desc"][:300],
                    source="rss",
                    site=feed.name,
                    published_at=item["published"],
                    source_document_id=raw_document_id,
                ))
            if state != "ok":
                status[feed.name] = state
            else:
                status[feed.name] = "ok" if matched else "ok (0 matched)"
        except Exception as exc:  # noqa: BLE001
            status[feed.name] = f"unavailable: {_feed_reason(exc)}"
            log.debug("RSS %s 解析失敗", feed.name, exc_info=True)
    # rss_feed 不在 web_search_research.SUPPORTED_SOURCES，故只保存 raw，不 materialize。
    log.info("RSS ticker=%s -> %d 筆", ticker, len(results))
    return results, status


# ---- 國際與地緣政治 -----------------------------------------------------

WORLD_WATCH_REL = "config/world_watch.json"


def load_world_watch(root: Optional[Path] = None) -> Dict[str, Any]:
    """讀 ``config/world_watch.json``；不存在時回傳空 leaders/topics。"""
    # root 是資料根目錄（測試或副本可能沒有 config/），找不到時退回程式所在 repo。
    repo = Path(__file__).resolve().parents[2]
    candidates = [Path(root) / WORLD_WATCH_REL] if root is not None else []
    candidates.append(repo / WORLD_WATCH_REL)
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        return {"leaders": {}, "topics": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"leaders": {}, "topics": []}
    data.setdefault("leaders", {})
    data.setdefault("topics", [])
    return data


def _flatten_leaders(leaders: Any) -> List[str]:
    if isinstance(leaders, dict):
        names = [n for v in leaders.values() for n in (v or [])]
    else:
        names = list(leaders or [])
    return [str(n).strip() for n in names if str(n).strip()]


def _term_hits(text: str, terms: List[str]) -> List[str]:
    """英文字詞邊界、不分大小寫；中文子字串。別名重疊（Trump 與 Donald Trump）只算一個。"""
    hits: List[str] = []
    for term in terms:
        if term in hits:
            continue
        if _is_ascii_text(term):
            ok = re.search(
                rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, re.IGNORECASE,
            ) is not None
        else:
            ok = term in text
        if ok:
            hits.append(term)
    return [
        h for h in hits
        if not any(h != o and h.lower() in o.lower() for o in hits)
    ]


def _published_epoch(value: str) -> float:
    if not value:
        return 0.0
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(value).timestamp()
    except Exception:
        pass
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except Exception:
        return 0.0


def search_world_news(
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    feeds: tuple[Feed, ...] = RSS_FEEDS,
    watch: Optional[Dict[str, Any]] = None,
    max_results: int = 30,
) -> tuple[List[SearchResult], Dict[str, str]]:
    """用領導人與主題關鍵字篩選國際/地緣政治新聞。

    分數 = 命中領導人數 * 2 + 命中主題數，>=2 才收；依分數降冪、發布時間新到舊排序，
    URL 去重。回傳 (results, status)，status 以 feed 名稱為 key。
    """
    log = logger or get_logger("web-search")
    sess = session or _new_session()
    cfg = watch if watch is not None else load_world_watch(root)
    leaders = _flatten_leaders(cfg.get("leaders"))
    topics = [str(t).strip() for t in (cfg.get("topics") or []) if str(t).strip()]
    status: Dict[str, str] = {}
    best: Dict[str, tuple[int, float, SearchResult]] = {}
    for feed in feeds:
        content, raw_document_id, state = _load_feed(feed, session=sess, root=root, log=log)
        if content is None:
            status[feed.name] = state
            continue
        try:
            kept = 0
            for item in parse_feed_items(content):
                if not item["title"] or not item["url"]:
                    continue
                hay = f"{item['title']} {item['desc']}"
                lead_hits = _term_hits(hay, leaders)
                topic_hits = _term_hits(hay, topics)
                score = len(lead_hits) * 2 + len(topic_hits)
                if score < 2:
                    continue
                kept += 1
                result = SearchResult(
                    title=item["title"],
                    url=item["url"],
                    snippet=f"[{', '.join(lead_hits + topic_hits)}] {item['desc'][:300]}".strip(),
                    source="rss_world",
                    site=feed.name,
                    published_at=item["published"],
                    source_document_id=raw_document_id,
                )
                key = (score, _published_epoch(item["published"]))
                prev = best.get(result.url)
                if prev is None or key > (prev[0], prev[1]):
                    best[result.url] = (score, key[1], result)
            status[feed.name] = state if state != "ok" else ("ok" if kept else "ok (0 matched)")
        except Exception as exc:  # noqa: BLE001
            status[feed.name] = f"unavailable: {_feed_reason(exc)}"
            log.debug("RSS world %s 解析失敗", feed.name, exc_info=True)
    ordered = sorted(best.values(), key=lambda t: (t[0], t[1]), reverse=True)
    results = [r for _, _, r in ordered[:max_results]]
    log.info("RSS world -> %d 筆", len(results))
    return results, status


# ----------------------------------------------------------------------
# 抓取單一網頁主要文字
# ----------------------------------------------------------------------


def fetch_page_text(
    url: str,
    *,
    max_chars: int = 4000,
    timeout: int = 12,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    root: Optional[Path] = None,
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
        if root is not None and getattr(resp, "content", b""):
            try:
                source_document = SourceRegistry(Path(root)).capture_document(
                    "web_page",
                    resp.content,
                    source_url=url,
                    source_ref=url,
                    title=url,
                    kind="page_response",
                    suffix="html",
                    mime_type="text/html; charset=utf-8",
                    metadata={"url": url, "max_chars": max_chars},
                )
                content.source_document_id = source_document.document_id
            except Exception:
                log.debug("網頁原始回應保存失敗: %s", url, exc_info=True)
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
        chunks: List[str] = []
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
        title_m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        if title_m:
            content.title = _clean_text(title_m.group(1))
        text = re.sub(r"<script.*?</script>", "", body, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
        text = _clean_text(text)
        content.text = text[:max_chars]
    return content


# ----------------------------------------------------------------------
# 個股自動研究 (組合)
# ----------------------------------------------------------------------


def _build_queries(ticker: str, name: str) -> List[str]:
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


def _cache_path(key: str, root: Optional[Path]) -> Path:
    today = now_tw().date().isoformat()
    return (root or Path.cwd()) / CACHE_DIR_REL / f"{key}_{today}.json"


def _load_cache(key: str, root: Optional[Path], max_age_hours: int) -> Optional[Dict[str, Any]]:
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


def _save_cache(key: str, root: Optional[Path], data: Dict[str, Any]) -> Path:
    p = _cache_path(key, root)
    mk_folder(str(p.parent))
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    mirror_file_to_cloud(p, root=root)
    return p


def research_ticker(
    ticker: str,
    *,
    name: str = "",
    root: Optional[Path] = None,
    max_pages: int = 5,
    max_results_per_query: int = 6,
    force_refresh: bool = False,
    max_age_hours: int = DEFAULT_AGE_HOURS,
    logger: Optional[logging.Logger] = None,
    english_name: str = "",
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
                    source_status=dict(cached.get("source_status") or {}),
                    world_results=[SearchResult(**r) for r in cached.get("world_results", []) or []],
                )
                log.info("[%s] web research 命中快取", ticker)
                return material
            except Exception:
                log.debug("[%s] web research 快取格式異常，重抓", ticker)

    sess = _new_session()
    queries = _build_queries(ticker, name)
    all_results: List[SearchResult] = []
    seen_urls: set[str] = set()

    source_status: Dict[str, str] = {}
    ddg_tripped = False
    news_count = 0
    for q in queries:
        for r in search_news(
            q, max_results=max_results_per_query, session=sess, logger=log, root=root,
        ):
            if r.url in seen_urls:
                continue
            seen_urls.add(r.url)
            all_results.append(r)
            news_count += 1
        time.sleep(0.5)
        if not ddg_tripped:
            try:
                for r in search_web(
                    q, max_results=max_results_per_query, session=sess, logger=log,
                    root=root, strict=True,
                ):
                    if r.url in seen_urls:
                        continue
                    seen_urls.add(r.url)
                    all_results.append(r)
                source_status.setdefault("ddg", "ok")
            except SearchUnavailable as exc:
                ddg_tripped = True
                source_status["ddg"] = f"unavailable: {exc}"
                log.warning("[%s] DDG 不可用，本輪略過: %s", ticker, exc)
        time.sleep(0.6)
    source_status["google_news"] = "ok" if news_count else "ok (0 results)"

    if not english_name:
        try:
            from bot.company_info import english_name_for

            english_name = english_name_for(ticker, root) or ""
        except Exception:
            english_name = ""
    rss_results, rss_status = search_feeds(
        ticker, name, english_name=english_name, session=sess, logger=log, root=root,
    )
    for feed_name, st in rss_status.items():
        source_status[f"rss:{feed_name}"] = st
    for r in rss_results:
        if r.url in seen_urls:
            continue
        seen_urls.add(r.url)
        all_results.append(r)

    world_results: List[SearchResult] = []
    try:
        world_results, world_status = search_world_news(session=sess, logger=log, root=root)
        if world_status and all(
            str(v).startswith("unavailable") for v in world_status.values()
        ):
            reasons = sorted({str(v).split("unavailable:", 1)[-1].strip() for v in world_status.values()})
            source_status["world"] = f"unavailable: all {len(world_status)} feeds failed ({reasons[0]})"
        else:
            source_status["world"] = f"ok ({len(world_results)})"
    except Exception as exc:  # noqa: BLE001
        source_status["world"] = f"unavailable: {type(exc).__name__}: {exc}"
        log.warning("[%s] 國際與地緣政治來源失敗: %s", ticker, exc)

    pages: List[PageContent] = []
    fetch_candidates = [r for r in all_results if r.source == "ddg"] + [
        r for r in all_results if r.source == "rss"
    ]
    for r in fetch_candidates:
        if not r.url.startswith("http"):
            continue
        host = _site_from_url(r.url)
        if any(b in host for b in ("twitter.com", "facebook.com", "youtube.com", "tiktok.com")):
            continue
        page = fetch_page_text(r.url, session=sess, logger=log, root=root)
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
        source_status=source_status,
        world_results=world_results,
    )
    try:
        _save_cache(key, root, {
            "ticker": material.ticker,
            "name": material.name,
            "queries": material.queries,
            "fetched_at": material.fetched_at,
            "search_results": [asdict(r) for r in material.search_results],
            "pages": [asdict(p) for p in material.pages],
            "source_status": material.source_status,
            "world_results": [asdict(r) for r in material.world_results],
        })
    except Exception:
        log.exception("[%s] web research 快取寫入失敗", ticker)
    return material


__all__ = [
    "Feed",
    "PageContent",
    "SearchResult",
    "WebMaterial",
    "fetch_page_text",
    "RSS_FEEDS",
    "SearchUnavailable",
    "research_ticker",
    "load_world_watch",
    "search_feeds",
    "search_news",
    "search_world_news",
    "search_web",
]

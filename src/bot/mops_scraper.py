"""mops_scraper -- 公開資訊觀測站 (MOPS) 法說會與重大訊息爬蟲。

提供：
* fetch_conference_schedule(year, month) -- 取得法人說明會行事曆
* fetch_material_info(ticker, year)      -- 取得個股重大訊息列表
* download_presentation(url, dest)       -- 下載法說會簡報 (PDF / PPTX)
* extract_pdf_text(path)                 -- 將 PDF 抽成純文字 (若有 pypdf)

爬蟲使用簡易 HTTP + 容錯 parser，不強制依賴 BeautifulSoup 與 pypdf；
若使用者已安裝會自動啟用更精細的解析。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.source_registry import SourceRegistry
from bot.stock_db import SourceDocumentRow
from bot.utils import get_logger, mk_folder

try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAS_BS4 = True
except Exception:
    _HAS_BS4 = False

try:
    from pypdf import PdfReader  # type: ignore
    _HAS_PYPDF = True
except Exception:
    try:
        from PyPDF2 import PdfReader  # type: ignore
        _HAS_PYPDF = True
    except Exception:
        _HAS_PYPDF = False


# ----------------------------------------------------------------------
# 資料模型
# ----------------------------------------------------------------------


@dataclass
class ConferenceEntry:
    """法說會行事曆單筆。"""

    date: dt.date
    time: str
    ticker: str
    company: str
    note: str = ""
    presentation_url: str = ""


@dataclass
class MaterialInfo:
    """重大訊息單筆。"""

    date: dt.date
    time: str
    ticker: str
    company: str
    subject: str
    detail_url: str = ""


@dataclass
class PresentationText:
    """法說會簡報抽出的純文字。"""

    ticker: str
    source: str  # url or local path
    text: str
    pages: int = 0
    extra: dict = field(default_factory=dict)
    source_document_id: str = ""
    research_status: str = "skipped"
    research_document_id: str = ""
    evidence_id: str = ""
    claim_id: str = ""
    research_run_id: str = ""
    research_error: str = ""


# ----------------------------------------------------------------------
# HTTP session
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# 主機設定
# ----------------------------------------------------------------------
#
# MOPS 於 2024-2025 改版後，舊網域 mops.twse.com.tw 的 ajax 端點會回
# 「FOR SECURITY REASONS, THIS PAGE CAN NOT BE ACCESSED」安全性阻擋頁。
# 目前可正常存取的主機為 mopsov.twse.com.tw (公開查詢介面)。
# 可用環境變數 MOPS_HOST 覆寫 (例如官方再次搬遷時)。
MOPS_HOST = os.environ.get("MOPS_HOST", "https://mopsov.twse.com.tw").rstrip("/")


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": f"{MOPS_HOST}/mops/web/index",
    })
    return s


def _is_security_block(html: str) -> bool:
    """偵測 MOPS 安全性阻擋頁 (HTTP 200 但無資料)。"""
    head = (html or "")[:400].upper()
    return "FOR SECURITY REASONS" in head or "CAN NOT BE ACCESSED" in head


def _capture_mops_response(
    source: str,
    content: bytes | bytearray | str,
    *,
    root: Optional[Path],
    source_url: str,
    source_ref: str,
    title: str,
    suffix: str,
    mime_type: str,
    as_of: str = "",
    quality_status: str = "ok",
    metadata: Optional[dict[str, Any]] = None,
    logger: logging.Logger,
) -> Optional[SourceDocumentRow]:
    """保存 MOPS 原始回應；保存失敗不阻斷既有解析流程。"""
    if not content:
        return None
    try:
        return SourceRegistry(root or Path.cwd()).capture_document(
            source,
            content,
            source_url=source_url,
            source_ref=source_ref,
            title=title,
            kind="http_response",
            suffix=suffix,
            mime_type=mime_type,
            as_of=as_of,
            quality_status=quality_status,
            metadata=metadata,
        )
    except Exception:
        logger.debug("MOPS 原始回應保存失敗: %s %s", source, source_ref, exc_info=True)
        return None


# ----------------------------------------------------------------------
# 法說會行事曆
# ----------------------------------------------------------------------

# MOPS 「投資人關係 → 法人說明會」公開查詢端點 (動態 JSP)。
# 走 mopsov 主機；若主管機關調整路徑，可用環境變數 MOPS_HOST 覆寫主機。
MOPS_CONF_URL = f"{MOPS_HOST}/mops/web/ajax_t100sb02_1"
MOPS_CALENDAR_FILE_PREFIX = "mops-calendar://"
MOPS_FILE_DOWNLOAD_PATH = "/server-java/FileDownLoad"
MOPS_FILE_DOWNLOAD_DIR = "/home/html/nas/STR/"


def fetch_conference_schedule(
    year_roc: int,
    month: int,
    logger: Optional[logging.Logger] = None,
    session: Optional[requests.Session] = None,
    *,
    root: Optional[Path] = None,
) -> List[ConferenceEntry]:
    """抓 MOPS 法說會行事曆 (民國年 + 月)。

    Args:
        year_roc: 民國年，例如 2026 西元 → 115
        month: 月份 1-12
    """
    log = logger or get_logger("mops")
    sess = session or _new_session()
    payload = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "TYPEK": "sii",
        "year": str(year_roc),
        "month": f"{month:02d}",
    }
    try:
        resp = sess.post(MOPS_CONF_URL, data=payload, timeout=15)
        resp.encoding = "utf-8"
        blocked = _is_security_block(resp.text)
        _capture_mops_response(
            "mops_conference_schedule",
            getattr(resp, "content", b"") or resp.text,
            root=root,
            source_url=MOPS_CONF_URL,
            source_ref=f"{year_roc}:{month:02d}",
            title=f"MOPS 法說會行事曆 {year_roc}/{month:02d}",
            suffix="html",
            mime_type="text/html; charset=utf-8",
            quality_status="invalid" if blocked else "ok",
            metadata={"year_roc": year_roc, "month": month},
            logger=log,
        )
        if blocked:
            log.warning(
                "MOPS 法說會行事曆被安全性阻擋 (%s)；可設定 MOPS_HOST 覆寫主機",
                MOPS_CONF_URL,
            )
            return []
        return _parse_conference_html(resp.text, year_roc, month, log)
    except Exception:
        log.exception("MOPS 法說會行事曆抓取失敗")
        return []


def _extract_calendar_pdf_filename(text: str) -> str:
    """從 MOPS 行事曆 fm_fileDownload onclick 抽出 PDF 檔名。"""
    if not text:
        return ""
    m = re.search(
        r"fm_fileDownload\.fileName\.value\s*=\s*[\"']([^\"']+)[\"']",
        text,
        re.I,
    )
    return m.group(1).strip() if m else ""


def _calendar_presentation_ref(filename: str) -> str:
    if not filename:
        return ""
    return f"{MOPS_CALENDAR_FILE_PREFIX}{filename}"


def is_meaningful_presentation_url(url: str) -> bool:
    """簡報連結是否為可下載/可開啟的有效 URL（排除僅 MOPS 主機根路徑）。"""
    u = (url or "").strip()
    if not u:
        return False
    if u.startswith(MOPS_CALENDAR_FILE_PREFIX):
        return True
    bare = u.rstrip("/")
    if bare in (
        "https://mopsov.twse.com.tw",
        "http://mopsov.twse.com.tw",
        "https://mops.twse.com.tw",
        "http://mops.twse.com.tw",
    ):
        return False
    if u.startswith("http"):
        return len(u) > len(MOPS_HOST) + 8
    return "://" in u


def _best_pdf_from_row_html(row_html: str) -> str:
    """從整列 HTML 挑選最佳簡報 PDF（優先 *M001.pdf）。"""
    candidates = re.findall(
        r"fm_fileDownload\.fileName\.value\s*=\s*[\"']([^\"']+)[\"']",
        row_html,
        re.I,
    )
    if not candidates:
        return ""
    for fn in candidates:
        if fn.upper().endswith("M001.PDF"):
            return fn
    return candidates[0]


def _presentation_date_hint(value: str) -> str:
    """從簡報檔名接受明確的西元年月日，不以模糊文字猜日期。"""
    match = re.search(
        r"(20\d{2})[-_/]?(\d{2})[-_/]?(\d{2})",
        str(value or ""),
    )
    if not match:
        return ""
    try:
        return dt.date(
            int(match.group(1)), int(match.group(2)), int(match.group(3)),
        ).isoformat()
    except ValueError:
        return ""


def _is_live_presentation_url(url: str) -> bool:
    """辨識直播連結；直播仍優先於同一列的下載檔案。"""
    value = str(url or "").strip().lower()
    return any(token in value for token in ("webcast", "live", "stream"))


def _is_pdf_presentation_url(url: str) -> bool:
    """只用 URL 明確的 PDF 標記，不把一般公司首頁當成簡報。"""
    value = str(url or "").strip().lower()
    return value.startswith(MOPS_CALENDAR_FILE_PREFIX) or ".pdf" in value


def _extract_url_from_onclick(text: str) -> str:
    """從 onclick / openWindow / window.open 字串抽出 URL。"""
    if not text:
        return ""
    pdf = _extract_calendar_pdf_filename(text)
    if pdf:
        return _calendar_presentation_ref(pdf)
    for pattern in (
        r"""openWindow\s*\(\s*\\?'([^'\\]+)\\?'""",
        r"""openWindow\s*\(\s*["']([^"']+)["']""",
        r"""window\.open\s*\(\s*\\?'([^'\\]+)\\?'""",
        r"""window\.open\s*\(\s*["']([^"']+)["']""",
        r"""location\.href\s*=\s*["']([^"']+)["']""",
    ):
        m = re.search(pattern, text, re.I)
        if m:
            url = m.group(1).strip()
            if url and not url.lower().startswith("javascript:"):
                return absolute_url(MOPS_HOST, url)
    return ""


def _url_from_tr_element(tr: Any) -> str:
    """從 <tr> 元素抽出簡報/詳情 URL (HTTP 直播 > PDF > href > onclick)。"""
    if not (_HAS_BS4 and tr is not None):
        return ""
    hrefs = [
        str(a.get("href", "")).strip()
        for a in tr.find_all("a", href=True)
        if str(a.get("href", "")).strip() not in ("", "#")
    ]
    for raw_href in hrefs:
        if _is_live_presentation_url(raw_href):
            return raw_href
    pdf = _best_pdf_from_row_html(str(tr))
    if pdf:
        return _calendar_presentation_ref(pdf)
    for raw_href in hrefs:
        if _is_pdf_presentation_url(raw_href):
            return raw_href
    onclick = str(tr.get("onclick", "") or "")
    if not onclick:
        for a in tr.find_all("a"):
            onclick = str(a.get("onclick", "") or "")
            if onclick:
                break
    onclick_url = _extract_url_from_onclick(onclick)
    if onclick_url and _is_pdf_presentation_url(onclick_url):
        return onclick_url
    for raw_href in hrefs:
        if not raw_href.lower().startswith("javascript:"):
            return absolute_url(MOPS_HOST, raw_href)
    return onclick_url


def _href_from_tr_html(tr_html: str) -> str:
    """從 <tr> 原始 HTML 抽出簡報 URL (regex fallback)。"""
    hrefs = [
        str(value).strip()
        for value in re.findall(r"""href=["']([^"']+)["']""", tr_html, re.I)
        if str(value).strip() not in ("", "#")
    ]
    for href in hrefs:
        if _is_live_presentation_url(href):
            return href
    pdf = _best_pdf_from_row_html(tr_html)
    if pdf:
        return _calendar_presentation_ref(pdf)
    for href in hrefs:
        if _is_pdf_presentation_url(href):
            return href if href.lower().startswith("http") else absolute_url(MOPS_HOST, href)
    onclick_url = _extract_url_from_onclick(tr_html)
    if onclick_url and _is_pdf_presentation_url(onclick_url):
        return onclick_url
    for href in hrefs:
        if not href.lower().startswith("javascript:"):
            return href if href.lower().startswith("http") else absolute_url(MOPS_HOST, href)
    return onclick_url


def _html_to_plain_text(html: str, *, max_chars: int = 20000) -> str:
    """將 MOPS 詳情頁 HTML 抽成純文字。"""
    if not html:
        return ""
    if _HAS_BS4:
        soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        parts: List[str] = []
        for tr in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            line = " | ".join(c for c in cells if c)
            if line:
                parts.append(line)
        if not parts:
            parts.append(soup.get_text("\n", strip=True))
        text = "\n".join(parts).strip()
    else:
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S | re.I)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        return text[:max_chars] + "\n...(已截斷)"
    return text


def _parse_conference_html(
    html: str,
    year_roc: int,
    month: int,
    logger: logging.Logger,
) -> List[ConferenceEntry]:
    """容錯解析 MOPS 表格 (BeautifulSoup 優先，否則用正則 fallback)。"""
    result: List[ConferenceEntry] = []
    rows: List[Tuple[List[str], str]] = []
    if _HAS_BS4:
        soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) >= 4:
                    rows.append((cells, _url_from_tr_element(tr)))
    else:
        for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
            tr_html = m.group(0)
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(1), re.S | re.I)
            cleaned = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if len(cleaned) >= 4:
                rows.append((cleaned, _href_from_tr_html(tr_html)))

    for cells, presentation_url in rows:
        date_str = next((c for c in cells if re.match(r"\d{2,4}/\d{1,2}/\d{1,2}", c)), "")
        if not date_str:
            continue
        try:
            parts = date_str.split("/")
            if len(parts) != 3:
                continue
            y = int(parts[0])
            if y < 1911:
                y += 1911
            d = dt.date(y, int(parts[1]), int(parts[2]))
        except Exception:
            continue

        ticker = ""
        company = ""
        for c in cells:
            if re.fullmatch(r"\d{4,6}[A-Z]?", c):
                ticker = c
                break
        for c in cells:
            if c and not re.fullmatch(r"\d{4,6}[A-Z]?", c) and "/" not in c and ":" not in c[:5]:
                if c != ticker and len(c) > 1:
                    company = c
                    break

        time_str = next((c for c in cells if re.match(r"\d{1,2}:\d{2}", c)), "")
        note = next(
            (c for c in cells if c not in (date_str, ticker, company, time_str)),
            "",
        )

        if ticker:
            result.append(ConferenceEntry(
                date=d, time=time_str, ticker=ticker,
                company=company, note=note,
                presentation_url=presentation_url,
            ))

    logger.info("MOPS 法說會行事曆 %d/%d -> %d 筆", year_roc, month, len(result))
    return result


# ----------------------------------------------------------------------
# 重大訊息
# ----------------------------------------------------------------------

MOPS_MATERIAL_URL = f"{MOPS_HOST}/mops/web/ajax_t05st02"
# TWSE OpenAPI「上市公司每日重大訊息」(公開、穩定 JSON；僅含最近交易日全市場)
URL_MATERIAL_OPENAPI = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"


def _bounded_request_timeout(
    timeout: float,
    deadline: Optional[float] = None,
) -> float:
    """Return a safe per-request timeout, respecting an optional deadline."""
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = 15.0
    value = max(0.25, min(value, 60.0))
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MOPS request deadline exceeded")
        value = min(value, max(0.25, remaining))
    return value


def _material_key(item: MaterialInfo) -> Tuple[str, str]:
    return (item.date.isoformat(), (item.subject or "").strip())


def _merge_material_lists(
    openapi_items: List[MaterialInfo],
    mops_items: List[MaterialInfo],
) -> List[MaterialInfo]:
    """合併 OpenAPI 與 mopsov 重訊；以 mopsov detail_url 補 OpenAPI 列。"""
    mops_by_key = {_material_key(m): m for m in mops_items}
    merged: List[MaterialInfo] = []
    seen: set[Tuple[str, str]] = set()

    for item in openapi_items:
        key = _material_key(item)
        seen.add(key)
        mops_match = mops_by_key.get(key)
        if mops_match and not item.detail_url and mops_match.detail_url:
            item = MaterialInfo(
                date=item.date,
                time=item.time or mops_match.time,
                ticker=item.ticker,
                company=item.company or mops_match.company,
                subject=item.subject,
                detail_url=mops_match.detail_url,
            )
        merged.append(item)

    for item in mops_items:
        key = _material_key(item)
        if key in seen:
            continue
        merged.append(item)
        seen.add(key)

    merged.sort(key=lambda m: (m.date, m.time), reverse=True)
    return merged


def _fetch_material_mops(
    ticker: str,
    year_roc: int,
    sess: requests.Session,
    log: logging.Logger,
    *,
    stats: Optional[Dict[str, Any]] = None,
    root: Optional[Path] = None,
    timeout: float = 15.0,
    deadline: Optional[float] = None,
) -> List[MaterialInfo]:
    payload = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "TYPEK": "sii",
        "co_id": ticker,
        "year": str(year_roc),
    }
    try:
        resp = sess.post(
            MOPS_MATERIAL_URL,
            data=payload,
            timeout=_bounded_request_timeout(timeout, deadline),
        )
        resp.encoding = "utf-8"
        blocked = _is_security_block(resp.text)
        _capture_mops_response(
            "mops_material_history",
            getattr(resp, "content", b"") or resp.text,
            root=root,
            source_url=MOPS_MATERIAL_URL,
            source_ref=f"{ticker}:{year_roc}",
            title=f"MOPS 重大訊息 {ticker} {year_roc}",
            suffix="html",
            mime_type="text/html; charset=utf-8",
            quality_status="invalid" if blocked else "ok",
            metadata={"ticker": ticker, "year_roc": year_roc},
            logger=log,
        )
        if blocked:
            log.warning("MOPS 重大訊息被安全性阻擋: %s (可設 MOPS_HOST 覆寫)", ticker)
            if stats is not None:
                stats["mops_blocked"] = True
            return []
        return _parse_material_html(resp.text, ticker, log)
    except Exception:
        log.exception("MOPS 重大訊息抓取失敗: %s", ticker)
        if stats is not None:
            stats["mops_error"] = True
        return []


def fetch_material_info(
    ticker: str,
    year_roc: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
    session: Optional[requests.Session] = None,
    *,
    merge_sources: bool = True,
    stats: Optional[Dict[str, Any]] = None,
    root: Optional[Path] = None,
    timeout: float = 15.0,
    deadline: Optional[float] = None,
) -> List[MaterialInfo]:
    """個股重大訊息查詢。

    預設合併兩來源：
    1. TWSE OpenAPI ``t187ap04_L`` — 當日重訊 (穩定，但常無 detail_url)
    2. mopsov ``ajax_t05st02`` — 歷史重訊 (含 detail_url)

    ``merge_sources=False`` 時維持舊行為：OpenAPI 有資料就直接回傳。
    """
    log = logger or get_logger("mops")
    sess = session or _new_session()
    if year_roc is None:
        year_roc = dt.date.today().year - 1911

    if stats is not None:
        stats.update({
            "openapi_count": 0,
            "mops_count": 0,
            "merged_count": 0,
            "with_detail_url": 0,
            "mops_blocked": False,
            "mops_error": False,
            "openapi_http_status": None,
            "openapi_valid": False,
            "openapi_row_count": 0,
            "openapi_error": "",
        })
    openapi_items = _fetch_material_openapi(
        ticker,
        sess,
        log,
        stats=stats,
        root=root,
        timeout=timeout,
        deadline=deadline,
    )
    if stats is not None:
        stats["openapi_count"] = len(openapi_items)
    if not merge_sources:
        if openapi_items:
            if stats is not None:
                stats["merged_count"] = len(openapi_items)
            return openapi_items
        mops_items = _fetch_material_mops(
            ticker,
            year_roc,
            sess,
            log,
            stats=stats,
            root=root,
            timeout=timeout,
            deadline=deadline,
        )
        if stats is not None:
            stats["mops_count"] = len(mops_items)
            stats["merged_count"] = len(mops_items)
        return mops_items

    mops_items = _fetch_material_mops(
        ticker,
        year_roc,
        sess,
        log,
        stats=stats,
        root=root,
        timeout=timeout,
        deadline=deadline,
    )
    if not mops_items and year_roc > 100:
        prev_items = _fetch_material_mops(
            ticker,
            year_roc - 1,
            sess,
            log,
            stats=stats,
            root=root,
            timeout=timeout,
            deadline=deadline,
        )
        if prev_items:
            log.info("重大訊息 %s 民國 %d 年無資料，改抓 %d 年 -> %d 筆",
                     ticker, year_roc, year_roc - 1, len(prev_items))
            mops_items = prev_items
            if stats is not None:
                stats["mops_year_fallback"] = year_roc - 1
    if stats is not None:
        stats["mops_count"] = len(mops_items)
    if openapi_items or mops_items:
        merged = _merge_material_lists(openapi_items, mops_items)
        if merged:
            log.info(
                "重大訊息合併 %s -> OpenAPI %d + MOPS %d = %d 筆",
                ticker, len(openapi_items), len(mops_items), len(merged),
            )
        if stats is not None:
            stats["merged_count"] = len(merged)
            stats["with_detail_url"] = sum(1 for m in merged if m.detail_url)
        return merged
    if stats is not None:
        stats["merged_count"] = 0
    return []


def _fetch_material_openapi(
    ticker: str,
    sess: requests.Session,
    log: logging.Logger,
    *,
    stats: Optional[Dict[str, Any]] = None,
    root: Optional[Path] = None,
    timeout: float = 20.0,
    deadline: Optional[float] = None,
) -> List[MaterialInfo]:
    """從 TWSE OpenAPI t187ap04_L 取該 ticker 的當日重大訊息。"""
    try:
        resp = sess.get(
            URL_MATERIAL_OPENAPI,
            timeout=_bounded_request_timeout(timeout, deadline),
        )
        if stats is not None:
            stats.update({
                "openapi_http_status": int(resp.status_code),
                "openapi_valid": False,
                "openapi_error": "",
            })
        _capture_mops_response(
            "twse_material_openapi",
            getattr(resp, "content", b"") or getattr(resp, "text", ""),
            root=root,
            source_url=URL_MATERIAL_OPENAPI,
            source_ref=ticker,
            title=f"TWSE 每日重大訊息 {ticker}",
            suffix="json",
            mime_type="application/json",
            quality_status="ok" if resp.status_code == 200 else "invalid",
            metadata={"ticker": ticker, "http_status": resp.status_code},
            logger=log,
        )
        if resp.status_code != 200:
            log.warning("重大訊息 OpenAPI HTTP %d", resp.status_code)
            if stats is not None:
                stats["openapi_error"] = f"HTTP {resp.status_code}"
            return []
        data = resp.json()
    except Exception as exc:
        log.debug("重大訊息 OpenAPI 抓取/解析失敗", exc_info=True)
        if stats is not None:
            stats["openapi_error"] = f"{type(exc).__name__}: {exc}"
        return []
    if not isinstance(data, list):
        if stats is not None:
            stats["openapi_error"] = "payload is not a list"
        return []
    if stats is not None:
        stats["openapi_valid"] = True
        stats["openapi_row_count"] = len(data)
    out: List[MaterialInfo] = []
    for row in data:
        code = str(row.get("公司代號") or "").strip()
        if code != ticker:
            continue
        d = _roc_to_date(str(row.get("發言日期") or row.get("出表日期") or ""))
        if d is None:
            continue
        # 欄位「主旨 」尾端可能帶空白
        subject = ""
        for k, v in row.items():
            if k.strip() == "主旨":
                subject = str(v).strip()
                break
        out.append(MaterialInfo(
            date=d,
            time=str(row.get("發言時間") or "").strip(),
            ticker=code,
            company=str(row.get("公司名稱") or "").strip(),
            subject=subject,
        ))
    if out:
        log.info("重大訊息 OpenAPI %s -> %d 筆 (當日)", ticker, len(out))
    return out


def _roc_to_date(s: str) -> Optional[dt.date]:
    """民國 YYYMMDD / 西元 YYYYMMDD / YYY/MM/DD → date。"""
    s = (s or "").strip()
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    try:
        if len(digits) == 8:  # 西元
            return dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        if len(digits) == 7:  # 民國
            return dt.date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7]))
    except ValueError:
        return None
    return None


def _parse_material_html(
    html: str,
    ticker: str,
    logger: logging.Logger,
) -> List[MaterialInfo]:
    result: List[MaterialInfo] = []
    rows: List[Tuple[List[str], str]] = []
    if _HAS_BS4:
        soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
        for tr in soup.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) >= 4:
                rows.append((cells, _url_from_tr_element(tr)))
    else:
        for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
            tr_html = m.group(0)
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(1), re.S | re.I)
            cleaned = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if len(cleaned) >= 4:
                rows.append((cleaned, _href_from_tr_html(tr_html)))

    for cells, detail_url in rows:
        date_str = next((c for c in cells if re.match(r"\d{2,4}/\d{1,2}/\d{1,2}", c)), "")
        if not date_str:
            continue
        try:
            parts = date_str.split("/")
            y = int(parts[0]) + (1911 if int(parts[0]) < 1911 else 0)
            d = dt.date(y, int(parts[1]), int(parts[2]))
        except Exception:
            continue
        time_str = next((c for c in cells if re.match(r"\d{1,2}:\d{2}", c)), "")
        subject = next(
            (c for c in cells if len(c) > 8 and c != date_str and c != time_str),
            "",
        )
        result.append(MaterialInfo(
            date=d, time=time_str, ticker=ticker,
            company="", subject=subject,
            detail_url=detail_url,
        ))

    logger.info("MOPS 重大訊息 %s -> %d 筆", ticker, len(result))
    return result


@dataclass
class MaterialDetailResult:
    """MOPS 重大訊息詳情抓取結果；可選擇帶 research 垂直切片。"""

    text: str = ""
    source_document_id: str = ""
    research_document_id: str = ""
    evidence_id: str = ""
    claim_id: str = ""
    run_id: str = ""
    slice: Optional[Dict[str, Any]] = None
    # skipped=未執行；ok/reused=切片成功；failed=切片失敗（仍可能有 raw text）
    research_status: str = "skipped"
    research_error: str = ""


def ingest_material_detail(
    detail_url: str,
    *,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    max_chars: int = 20000,
    root: Optional[Path] = None,
    ticker: str = "",
    materialize_research: bool = True,
    timeout: float = 20.0,
) -> MaterialDetailResult:
    """抓取 MOPS 詳情並（可選）完成官方垂直切片。

    垂直切片僅在 ``root`` 可寫且 raw ``source_document`` 成功登錄後執行；
    不會依 URL／標題猜測 ``source_document_id``。切片失敗時
    ``research_status=failed`` 並保留 ``research_error``，不靜默吞掉。
    """
    if not detail_url:
        return MaterialDetailResult(research_status="skipped", research_error="缺少 detail_url")
    log = logger or get_logger("mops")
    sess = session or _new_session()
    try:
        resp = sess.get(
            detail_url,
            timeout=_bounded_request_timeout(timeout),
        )
        resp.encoding = "utf-8"
        blocked = _is_security_block(resp.text)
        source_doc = _capture_mops_response(
            "mops_material_detail",
            getattr(resp, "content", b"") or resp.text,
            root=root,
            source_url=detail_url,
            source_ref=detail_url,
            title="MOPS 重大訊息詳情",
            suffix="html",
            mime_type="text/html; charset=utf-8",
            quality_status="invalid" if blocked else "ok",
            metadata={"ticker": ticker} if ticker else None,
            logger=log,
        )
        if blocked:
            log.warning("MOPS 詳情被安全性阻擋: %s", detail_url)
            return MaterialDetailResult(
                source_document_id=source_doc.document_id if source_doc else "",
                research_status="skipped",
                research_error="MOPS 詳情被安全性阻擋",
            )
        text = _html_to_plain_text(resp.text, max_chars=max_chars)
        result = MaterialDetailResult(
            text=text,
            source_document_id=source_doc.document_id if source_doc else "",
            research_status="skipped",
        )
        if not materialize_research:
            result.research_error = "materialize_research=False"
            return result
        if root is None:
            result.research_error = "缺少 root，無法寫入 research slice"
            return result
        if source_doc is None:
            result.research_status = "failed"
            result.research_error = "raw source_document 登錄失敗"
            return result
        if not text.strip():
            result.research_status = "failed"
            result.research_error = "詳情正文為空，無法建立 research slice"
            return result
        if (source_doc.quality_status or "ok") not in {"ok", "partial"}:
            result.research_status = "failed"
            result.research_error = (
                f"source_document quality_status={source_doc.quality_status} 不可切片"
            )
            return result
        from bot.mops_research import materialize_mops_detail_slice

        slice_row = materialize_mops_detail_slice(
            root=Path(root),
            source_document_id=source_doc.document_id,
            canonical_text=text,
            detail_url=detail_url,
            ticker=ticker,
            title=source_doc.title or "MOPS 重大訊息詳情",
        )
        result.research_status = slice_row.status
        result.research_error = slice_row.error
        result.slice = slice_row.to_dict()
        if slice_row.status in {"ok", "reused"}:
            result.research_document_id = slice_row.research_document_id
            result.evidence_id = slice_row.evidence_id
            result.claim_id = slice_row.claim_id
            result.run_id = slice_row.run_id
        else:
            log.warning(
                "MOPS research 垂直切片失敗: %s (%s)",
                detail_url, slice_row.error or slice_row.status,
            )
        return result
    except Exception as exc:
        log.exception("MOPS 詳情抓取失敗: %s", detail_url)
        return MaterialDetailResult(
            research_status="failed",
            research_error=f"{type(exc).__name__}: {exc}",
        )


def fetch_material_detail(
    detail_url: str,
    *,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    max_chars: int = 20000,
    root: Optional[Path] = None,
) -> str:
    """抓取 MOPS 重大訊息詳情頁並抽成純文字。"""
    return ingest_material_detail(
        detail_url,
        session=session,
        logger=logger,
        max_chars=max_chars,
        root=root,
        materialize_research=True,
    ).text


# ----------------------------------------------------------------------
# 簡報下載 / PDF 文字抽取
# ----------------------------------------------------------------------


def download_file(
    url: str,
    dest: Path,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    *,
    root: Optional[Path] = None,
) -> Optional[Path]:
    """串流下載檔案。"""
    log = logger or get_logger("mops")
    sess = session or _new_session()
    restore_file_from_cloud(dest)
    if dest.exists():
        return dest
    try:
        mk_folder(str(dest.parent))
        with sess.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            raw_chunks: List[bytes] = []
            with dest.open("wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
                        raw_chunks.append(chunk)
            payload = b"".join(raw_chunks)
            content_type = str(
                getattr(r, "headers", {}).get("Content-Type", "") or ""
            )
            is_pdf = payload.startswith(b"%PDF-")
            is_html = "html" in content_type.lower() or payload.lstrip().lower().startswith(
                (b"<html", b"<!doctype")
            ) or payload.startswith((b"\xff\xfe<", b"\xfe\xff\x00<"))
            _capture_mops_response(
                "mops_presentation",
                payload,
                root=root,
                source_url=url,
                source_ref=url,
                title=dest.name,
                suffix=(
                    "pdf" if is_pdf else "html" if is_html else
                    "txt" if "text/" in content_type.lower() and dest.suffix.lower() == ".pdf"
                    else dest.suffix.lstrip(".") or "bin"
                ),
                mime_type=content_type,
                as_of=_presentation_date_hint(dest.name),
                metadata={"destination": str(dest)},
                logger=log,
            )
        log.info("下載完成: %s -> %s", url, dest)
        mirror_file_to_cloud(dest)
        return dest
    except Exception:
        log.exception("檔案下載失敗: %s", url)
        return None


def download_calendar_file(
    filename: str,
    dest: Path,
    *,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    root: Optional[Path] = None,
) -> Optional[Path]:
    """下載 MOPS 行事曆 PDF（POST ``fm_fileDownload``）。"""
    log = logger or get_logger("mops")
    sess = session or _new_session()
    restore_file_from_cloud(dest)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = absolute_url(MOPS_HOST, MOPS_FILE_DOWNLOAD_PATH)
    data = {
        "step": "9",
        "filePath": MOPS_FILE_DOWNLOAD_DIR,
        "fileName": filename,
        "functionName": "t100sb02_1",
    }
    try:
        mk_folder(str(dest.parent))
        resp = sess.post(url, data=data, timeout=60)
        resp.raise_for_status()
        if not resp.content or len(resp.content) < 100:
            log.warning("MOPS 行事曆 PDF 下載過小: %s", filename)
            return None
        _capture_mops_response(
            "mops_calendar_pdf",
            resp.content,
            root=root,
            source_url=url,
            source_ref=filename,
            title=filename,
            suffix="pdf",
            mime_type="application/pdf",
            as_of=_presentation_date_hint(filename),
            metadata={"filename": filename},
            logger=log,
        )
        dest.write_bytes(resp.content)
        mirror_file_to_cloud(dest)
        log.info("MOPS 行事曆 PDF 下載完成: %s -> %s", filename, dest)
        return dest
    except Exception:
        log.exception("MOPS 行事曆 PDF 下載失敗: %s", filename)
        return None


def _materialize_presentation_research(
    presentation: PresentationText,
    path: Path,
    *,
    root: Optional[Path],
    source_ref: str = "",
    logger: Optional[logging.Logger] = None,
) -> PresentationText:
    """由下載檔案的 exact bytes 找到 manifest，再建立 PDF research slice。

    這裡只用 SHA-256 與既有 source registry 精確比對；找不到 manifest 時
    不依 URL、檔名或文字猜測來源，而是保留抽取結果並標成 skipped。
    """
    log = logger or get_logger("mops")
    if root is None:
        presentation.research_status = "skipped"
        presentation.research_error = "缺少 root，無法寫入 research slice"
        return presentation
    try:
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        with SourceRegistry(Path(root)) as registry:
            candidates = [
                document
                for document in registry.db.list_source_documents(limit=5000)
                if document.source in {"mops_presentation", "mops_calendar_pdf"}
                and document.content_hash == digest
            ]
            if source_ref:
                preferred = [
                    document for document in candidates
                    if document.source_ref == source_ref
                    or document.url == source_ref
                    or document.title == path.name
                ]
                candidates = preferred or candidates
            source = candidates[0] if candidates else None
        if source is None:
            presentation.research_status = "skipped"
            presentation.research_error = "raw source_document 不存在，未猜測來源"
            return presentation

        from bot.mops_presentation_research import materialize_mops_presentation_slice

        sliced = materialize_mops_presentation_slice(
            root=Path(root),
            source_document_id=source.document_id,
            canonical_text=presentation.text,
            ticker=presentation.ticker,
            title=source.title or path.name,
            pages=presentation.pages,
            extraction_method="pypdf" if path.suffix.lower() == ".pdf" else "text",
            conference_date=_presentation_date_hint(path.name) or str(source.as_of or ""),
            calendar_conference_date=_presentation_date_hint(path.name),
        )
        presentation.source_document_id = source.document_id
        presentation.research_status = sliced.status
        presentation.research_document_id = sliced.research_document_id
        presentation.evidence_id = sliced.evidence_id
        presentation.claim_id = sliced.claim_id
        presentation.research_run_id = sliced.run_id
        presentation.research_error = sliced.error
        if sliced.status not in {"ok", "reused"}:
            log.warning(
                "MOPS 法說簡報 research slice %s: %s",
                sliced.status, sliced.error,
            )
        return presentation
    except Exception as exc:  # noqa: BLE001
        presentation.research_status = "failed"
        presentation.research_error = f"{type(exc).__name__}: {exc}"
        log.exception("MOPS 法說簡報 research slice 失敗")
        return presentation


def download_presentation(
    url: str,
    dest: Path,
    *,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    root: Optional[Path] = None,
) -> Optional[PresentationText]:
    """下載法說會簡報 (PDF/文字) 並盡量抽出純文字。"""
    if url.startswith(MOPS_CALENDAR_FILE_PREFIX):
        filename = url[len(MOPS_CALENDAR_FILE_PREFIX):]
        if not dest.suffix:
            dest = dest.with_suffix(".pdf")
        path = download_calendar_file(
            filename, dest, session=session, logger=logger, root=root,
        )
    else:
        path = download_file(url, dest, session=session, logger=logger, root=root)
    if path is None:
        return None
    if path.suffix.lower() == ".pdf":
        try:
            is_pdf_payload = path.read_bytes().startswith(b"%PDF-")
        except OSError:
            is_pdf_payload = False
        presentation = extract_pdf_text(path)
        if presentation is None and not is_pdf_payload:
            # MOPS occasionally links to an HTML investor-relations page while
            # the calendar filename/destination still ends in .pdf.  Inspect
            # the bytes, but keep the result out of the verified presentation
            # chain; the raw HTML is a different document type.
            fallback_text = _read_presentation_text_fallback(path)
            if not fallback_text:
                return None
            presentation = PresentationText(
                ticker=path.stem.split("_")[0],
                source=str(path),
                text=fallback_text,
                extra={"content_fallback": "html_or_text"},
            )
            # Keep the exact manifest linkage when available.  The research
            # slice will reject the HTML source as a failed presentation, so
            # callers can distinguish "not a PDF" from "manifest missing".
            return _materialize_presentation_research(
                presentation,
                path,
                root=root,
                source_ref=(url if not url.startswith(MOPS_CALENDAR_FILE_PREFIX) else url[len(MOPS_CALENDAR_FILE_PREFIX):]),
                logger=logger,
            )
        if presentation is None:
            return None
        return _materialize_presentation_research(
            presentation,
            path,
            root=root,
            source_ref=(url if not url.startswith(MOPS_CALENDAR_FILE_PREFIX) else url[len(MOPS_CALENDAR_FILE_PREFIX):]),
            logger=logger,
        )
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return None
    if not text:
        return None
    ticker = path.stem.split("_")[0]
    presentation = PresentationText(
        ticker=ticker,
        source=str(path),
        text=text,
    )
    return _materialize_presentation_research(
        presentation,
        path,
        root=root,
        source_ref=url,
        logger=logger,
    )


def _read_presentation_text_fallback(path: Path, *, max_chars: int = 200_000) -> str:
    """讀取被錯誤命名為 .pdf 的 HTML／文字簡報頁。

    `%PDF` 開頭的內容不在這裡假裝成文字；它應由真正的 PDF extractor
    處理。非 PDF 內容則依 BOM 解碼，HTML 再套用既有 MOPS plain-text parser。
    """
    try:
        payload = path.read_bytes()
    except OSError:
        return ""
    if payload.startswith(b"%PDF"):
        return ""
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    try:
        text = payload.decode(encoding, errors="replace").strip()
    except Exception:
        return ""
    if not text:
        return ""
    printable = sum(1 for char in text if char.isprintable() or char.isspace())
    if printable / max(1, len(text)) < 0.65:
        return ""
    if "<html" in text.lower() or "<body" in text.lower() or "<table" in text.lower():
        return _html_to_plain_text(text, max_chars=max_chars)
    return text[:max_chars]


def extract_pdf_text(path: Path, max_pages: int = 100) -> Optional[PresentationText]:
    """抽 PDF 全文。若未安裝 pypdf 則回 None。"""
    if not _HAS_PYPDF:
        return None
    restore_file_from_cloud(path)
    try:
        reader = PdfReader(str(path))
        pages = min(len(reader.pages), max_pages)
        text_parts: List[str] = []
        for i in range(pages):
            try:
                text_parts.append(reader.pages[i].extract_text() or "")
            except Exception:
                continue
        text = "\n\n".join(text_parts).strip()
        return PresentationText(
            ticker=path.stem.split("_")[0],
            source=str(path),
            text=text,
            pages=pages,
        )
    except Exception:
        return None


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except Exception:
        return False


def absolute_url(base: str, rel: str) -> str:
    return urljoin(base, rel)


__all__ = [
    "ConferenceEntry",
    "MaterialDetailResult",
    "MaterialInfo",
    "PresentationText",
    "MOPS_CALENDAR_FILE_PREFIX",
    "absolute_url",
    "download_calendar_file",
    "download_file",
    "download_presentation",
    "is_meaningful_presentation_url",
    "extract_pdf_text",
    "fetch_conference_schedule",
    "fetch_material_detail",
    "fetch_material_info",
    "ingest_material_detail",
]

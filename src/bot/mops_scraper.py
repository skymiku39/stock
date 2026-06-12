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
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urljoin

import requests

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
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
        if _is_security_block(resp.text):
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
    for a in tr.find_all("a", href=True):
        raw_href = str(a.get("href", "")).strip()
        if raw_href.lower().startswith("http") and raw_href not in ("#", ""):
            return raw_href
    pdf = _best_pdf_from_row_html(str(tr))
    if pdf:
        return _calendar_presentation_ref(pdf)
    for a in tr.find_all("a", href=True):
        raw_href = str(a.get("href", "")).strip()
        if raw_href and raw_href not in ("#", "") and not raw_href.lower().startswith("javascript:"):
            return absolute_url(MOPS_HOST, raw_href)
    onclick = str(tr.get("onclick", "") or "")
    if not onclick:
        for a in tr.find_all("a"):
            onclick = str(a.get("onclick", "") or "")
            if onclick:
                break
    return _extract_url_from_onclick(onclick)


def _href_from_tr_html(tr_html: str) -> str:
    """從 <tr> 原始 HTML 抽出簡報 URL (regex fallback)。"""
    m = re.search(r"""href=["'](https?://[^"']+)["']""", tr_html, re.I)
    if m:
        return m.group(1).strip()
    pdf = _best_pdf_from_row_html(tr_html)
    if pdf:
        return _calendar_presentation_ref(pdf)
    m = re.search(r"""href=["']([^"'#][^"']*)["']""", tr_html, re.I)
    if m:
        href = m.group(1).strip()
        if href and not href.lower().startswith("javascript:"):
            return absolute_url(MOPS_HOST, href)
    return _extract_url_from_onclick(tr_html)


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
        resp = sess.post(MOPS_MATERIAL_URL, data=payload, timeout=15)
        resp.encoding = "utf-8"
        if _is_security_block(resp.text):
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

    openapi_items = _fetch_material_openapi(ticker, sess, log)
    if stats is not None:
        stats.update({
            "openapi_count": len(openapi_items),
            "mops_count": 0,
            "merged_count": 0,
            "with_detail_url": 0,
            "mops_blocked": False,
            "mops_error": False,
        })
    if not merge_sources:
        if openapi_items:
            if stats is not None:
                stats["merged_count"] = len(openapi_items)
            return openapi_items
        mops_items = _fetch_material_mops(
            ticker, year_roc, sess, log, stats=stats,
        )
        if stats is not None:
            stats["mops_count"] = len(mops_items)
            stats["merged_count"] = len(mops_items)
        return mops_items

    mops_items = _fetch_material_mops(
        ticker, year_roc, sess, log, stats=stats,
    )
    if not mops_items and year_roc > 100:
        prev_items = _fetch_material_mops(
            ticker, year_roc - 1, sess, log, stats=stats,
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
) -> List[MaterialInfo]:
    """從 TWSE OpenAPI t187ap04_L 取該 ticker 的當日重大訊息。"""
    try:
        resp = sess.get(URL_MATERIAL_OPENAPI, timeout=20)
        if resp.status_code != 200:
            log.warning("重大訊息 OpenAPI HTTP %d", resp.status_code)
            return []
        data = resp.json()
    except Exception:
        log.debug("重大訊息 OpenAPI 抓取/解析失敗", exc_info=True)
        return []
    out: List[MaterialInfo] = []
    for row in data if isinstance(data, list) else []:
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


def fetch_material_detail(
    detail_url: str,
    *,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    max_chars: int = 20000,
) -> str:
    """抓取 MOPS 重大訊息詳情頁並抽成純文字。"""
    if not detail_url:
        return ""
    log = logger or get_logger("mops")
    sess = session or _new_session()
    try:
        resp = sess.get(detail_url, timeout=20)
        resp.encoding = "utf-8"
        if _is_security_block(resp.text):
            log.warning("MOPS 詳情被安全性阻擋: %s", detail_url)
            return ""
        return _html_to_plain_text(resp.text, max_chars=max_chars)
    except Exception:
        log.exception("MOPS 詳情抓取失敗: %s", detail_url)
        return ""


# ----------------------------------------------------------------------
# 簡報下載 / PDF 文字抽取
# ----------------------------------------------------------------------


def download_file(
    url: str,
    dest: Path,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
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
            with dest.open("wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
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
        dest.write_bytes(resp.content)
        mirror_file_to_cloud(dest)
        log.info("MOPS 行事曆 PDF 下載完成: %s -> %s", filename, dest)
        return dest
    except Exception:
        log.exception("MOPS 行事曆 PDF 下載失敗: %s", filename)
        return None


def download_presentation(
    url: str,
    dest: Path,
    *,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[PresentationText]:
    """下載法說會簡報 (PDF/文字) 並盡量抽出純文字。"""
    if url.startswith(MOPS_CALENDAR_FILE_PREFIX):
        filename = url[len(MOPS_CALENDAR_FILE_PREFIX):]
        if not dest.suffix:
            dest = dest.with_suffix(".pdf")
        path = download_calendar_file(
            filename, dest, session=session, logger=logger,
        )
    else:
        path = download_file(url, dest, session=session, logger=logger)
    if path is None:
        return None
    if path.suffix.lower() == ".pdf":
        return extract_pdf_text(path)
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return None
    if not text:
        return None
    ticker = path.stem.split("_")[0]
    return PresentationText(
        ticker=ticker,
        source=str(path),
        text=text,
    )


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
]

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
from typing import List, Optional
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


def _parse_conference_html(
    html: str,
    year_roc: int,
    month: int,
    logger: logging.Logger,
) -> List[ConferenceEntry]:
    """容錯解析 MOPS 表格 (BeautifulSoup 優先，否則用正則 fallback)。"""
    result: List[ConferenceEntry] = []
    rows: List[List[str]] = []
    if _HAS_BS4:
        soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) >= 4:
                    rows.append(cells)
    else:
        for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(1), re.S | re.I)
            cleaned = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if len(cleaned) >= 4:
                rows.append(cleaned)

    for cells in rows:
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
            ))

    logger.info("MOPS 法說會行事曆 %d/%d -> %d 筆", year_roc, month, len(result))
    return result


# ----------------------------------------------------------------------
# 重大訊息
# ----------------------------------------------------------------------

MOPS_MATERIAL_URL = f"{MOPS_HOST}/mops/web/ajax_t05st02"
# TWSE OpenAPI「上市公司每日重大訊息」(公開、穩定 JSON；僅含最近交易日全市場)
URL_MATERIAL_OPENAPI = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"


def fetch_material_info(
    ticker: str,
    year_roc: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
    session: Optional[requests.Session] = None,
) -> List[MaterialInfo]:
    """個股重大訊息查詢。

    來源優先序：
    1. TWSE OpenAPI ``t187ap04_L`` (上市公司每日重大訊息)：公開、穩定 JSON，
       但只含「最近交易日」全市場資料 → 篩出該 ticker 當日訊息。
    2. mopsov 個股歷史查詢 ``ajax_t05st02``：可查歷史，但 MOPS 偶有阻擋/查無資料。

    兩者皆失敗時回空清單 (呼叫端可用長度 0 + log 判斷)。
    """
    log = logger or get_logger("mops")
    sess = session or _new_session()

    # ---- 1. OpenAPI 每日重大訊息 ----
    out = _fetch_material_openapi(ticker, sess, log)
    if out:
        return out

    # ---- 2. mopsov 個股歷史 fallback ----
    if year_roc is None:
        year_roc = dt.date.today().year - 1911
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
            return []
        return _parse_material_html(resp.text, ticker, log)
    except Exception:
        log.exception("MOPS 重大訊息抓取失敗: %s", ticker)
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
    rows: List[List[str]] = []
    if _HAS_BS4:
        soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
        for tr in soup.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) >= 4:
                rows.append(cells)
    else:
        for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(1), re.S | re.I)
            cleaned = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if len(cleaned) >= 4:
                rows.append(cleaned)

    for cells in rows:
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
        ))

    logger.info("MOPS 重大訊息 %s -> %d 筆", ticker, len(result))
    return result


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
    "absolute_url",
    "download_file",
    "extract_pdf_text",
    "fetch_conference_schedule",
    "fetch_material_info",
]

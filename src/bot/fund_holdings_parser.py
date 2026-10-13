"""fund_holdings_parser -- 共同基金持股頁的確定性解析（免 LLM）。

目前支援：
* MoneyDJ 境內基金持股頁
  `https://www.moneydj.com/funddj/yp/yp013000.djhtm?a=<fund_id>`

設計：
* 優先解析 HTML table（雙欄：左/右各「名稱、千股、比例、增減」）
* 公司名 → 證券代號透過 `company_info.lookup_symbol_by_name`
* 查不到代號時仍保留名稱，ticker 暫用公司名（與海外 ETF 退化策略一致）
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from bot.active_etf import Holding
from bot.company_info import load_company_map, lookup_symbol_by_name
from bot.utils import get_logger

try:
    from bs4 import BeautifulSoup  # type: ignore
    _HAS_BS4 = True
except Exception:
    _HAS_BS4 = False


_MONEYDJ_FUND_HOSTS = ("moneydj.com", "www.moneydj.com", "wwwfund.capital.com.tw")
_MONEYDJ_FUND_PATH_MARKERS = ("/funddj/yp/yp013000", "/w/wr/wr04.djhtm")


@dataclass
class ParsedFundHoldings:
    """共同基金持股解析結果。"""

    holdings: List[Holding]
    as_of: Optional[dt.date] = None
    source: str = "moneydj_fund"


def is_moneydj_fund_holdings_url(url: str) -> bool:
    """判斷 URL 是否為 MoneyDJ / FundDJ 共同基金持股頁。"""
    u = (url or "").strip().lower()
    if not u.startswith("http"):
        return False
    parsed = urlparse(u)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if not any(h in host for h in ("moneydj.com", "capital.com.tw")):
        return False
    return any(m in path for m in _MONEYDJ_FUND_PATH_MARKERS) or (
        "yp013000" in path or "wr04.djhtm" in path
    )


def _parse_date(text: str) -> Optional[dt.date]:
    """從『資料月份：2026/06/30』或『資料日期：2026/06/30』抽出日期。"""
    for pat in (
        r"資料月份[：:]\s*(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})",
        r"資料日期[：:]\s*(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})",
        r"資料月份[：:]\s*(\d{4})[/\-](\d{1,2})",
    ):
        m = re.search(pat, text)
        if not m:
            continue
        y = int(m.group(1))
        mo = int(m.group(2))
        if len(m.groups()) >= 3:
            d = int(m.group(3))
        else:
            # 僅有年月 → 取該月最後一天（以 28 起算再往上夾）
            d = 28
            for trial in (31, 30, 29, 28):
                try:
                    return dt.date(y, mo, trial)
                except ValueError:
                    continue
        try:
            return dt.date(y, mo, d)
        except ValueError:
            continue
    return None


def _to_float(raw: str) -> float:
    s = str(raw or "").strip().replace(",", "").replace("%", "")
    if not s or s in ("-", "—", "N/A", "n/a"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _row_pairs(cells: List[str]) -> List[Tuple[str, float, float]]:
    """從 8 欄列抽出左右兩組 (name, shares_千股, weight_pct)。"""
    out: List[Tuple[str, float, float]] = []
    # 標準：名稱 / 千股 / 比例 / 增減 × 2
    if len(cells) >= 8:
        chunks = (cells[0:4], cells[4:8])
    elif len(cells) >= 4:
        chunks = (cells[0:4],)
    else:
        return out
    for chunk in chunks:
        name = str(chunk[0] or "").strip()
        if not name or name in ("投資名稱", "股票名稱"):
            continue
        if any(x in name for x in ("自104", "公布基金", "附註", "資料月份", "資料日期")):
            continue
        shares_k = _to_float(chunk[1])
        weight = _to_float(chunk[2])
        if weight <= 0 and shares_k <= 0:
            continue
        out.append((name, shares_k, weight))
    return out


def parse_moneydj_fund_holdings_html(
    html: str,
    *,
    root: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
    company_map: Optional[Dict] = None,
) -> ParsedFundHoldings:
    """解析 MoneyDJ 共同基金持股 HTML → Holding 清單。"""
    log = logger or get_logger("fund-holdings")
    if not html or not _HAS_BS4:
        return ParsedFundHoldings(holdings=[])

    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text("\n", strip=True)
    as_of = _parse_date(page_text)

    cmap = company_map
    if cmap is None:
        try:
            cmap = load_company_map(root=root, logger=log)
        except Exception:
            log.debug("load_company_map 失敗，改以公司名作為 ticker", exc_info=True)
            cmap = {}

    def _has_holdings_header(table) -> bool:
        for tr in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            if cells[0] in ("投資名稱", "股票名稱"):
                return True
            # 雙欄表頭有時被攤平在同一列
            if "投資名稱" in cells or "股票名稱" in cells:
                return True
        return False

    seen: Dict[str, Holding] = {}
    for table in soup.find_all("table"):
        # MoneyDJ 常把持股表再包一層 outer table；略過含巢狀持股表的外殼，避免雙重累加。
        if any(_has_holdings_header(t) for t in table.find_all("table")):
            continue
        if not _has_holdings_header(table):
            continue
        for tr in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            # 略過表頭列
            if cells[0] in ("投資名稱", "股票名稱"):
                continue
            for name, shares_k, weight in _row_pairs(cells):
                clean_name = name.rstrip("*＊").strip()
                ticker = lookup_symbol_by_name(
                    clean_name, root=root, logger=log, company_map=cmap,
                ) or clean_name
                # MoneyDJ 單位為千股 → 轉成股，方便與其他來源對齊
                shares = shares_k * 1000.0
                key = ticker
                prev = seen.get(key)
                if prev is None:
                    seen[key] = Holding(
                        ticker=ticker,
                        name=clean_name,
                        weight_pct=weight,
                        shares=shares,
                        value=0.0,
                    )
                else:
                    # 同名合併權重/股數（僅在同頁真的重複列時）
                    prev.weight_pct += weight
                    prev.shares += shares

    holdings = sorted(seen.values(), key=lambda h: h.weight_pct, reverse=True)
    return ParsedFundHoldings(holdings=holdings, as_of=as_of, source="moneydj_fund")


def moneydj_fund_id_from_url(url: str) -> str:
    """從 URL 抽出基金 id（如 acdd04）；失敗回空字串。"""
    try:
        q = parse_qs(urlparse(url).query)
        raw = (q.get("a") or [""])[0]
        return str(raw).strip()
    except Exception:
        return ""


__all__ = [
    "ParsedFundHoldings",
    "is_moneydj_fund_holdings_url",
    "moneydj_fund_id_from_url",
    "parse_moneydj_fund_holdings_html",
]

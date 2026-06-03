"""company_info -- 上市/上櫃公司基本資料 (名稱 / 簡稱 / 產業別 / 上市日)。

問題背景
========
`stock_info` 過去只有少數手動輸入的列，導致「目前持股分析」與各種查資料頁面
大量股票顯示「未分類」、名稱空白 (分類出不來)。根本原因是**沒有自動化的公司
基本資料來源**，且官方資料的「產業別」是**數字代碼**而非中文 (格式不符)。

本模組
======
* 從 TWSE / TPEx 公開 OpenAPI 抓「公司基本資料」(免登入、穩定 JSON)。
* 將「產業別代碼」對應成中文 (`INDUSTRY_CODE_MAP`)。
* 提供 `lookup_company_info()` 單檔查詢 (程序內 + 每日檔案快取)。
* 提供 `backfill_stock_info()` 一次補滿整張 `stock_info`。

資料源
======
* 上市: https://openapi.twse.com.tw/v1/opendata/t187ap03_L
* 上櫃: https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

import requests

from bot.stock_db import StockInfo
from bot.utils import get_logger, mk_folder, now_tw

URL_TWSE_COMPANY = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
URL_TPEX_COMPANY = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

# 上市/上櫃「產業別」代碼 → 中文 (MOPS t187ap03 分類，TWSE 與 TPEx 共用同一套)。
INDUSTRY_CODE_MAP: Dict[str, str] = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "07": "化學生技醫療", "08": "玻璃陶瓷",
    "09": "造紙工業", "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業",
    "13": "電子工業", "14": "建材營造", "15": "航運業", "16": "觀光餐旅",
    "17": "金融保險業", "18": "貿易百貨", "19": "綜合", "20": "其他業",
    "21": "化學工業", "22": "生技醫療業", "23": "油電燃氣業", "24": "半導體業",
    "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業", "28": "電子零組件業",
    "29": "電子通路業", "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業",
    "33": "農業科技業", "34": "電子商務", "35": "綠能環保", "36": "數位雲端",
    "37": "運動休閒", "38": "居家生活", "80": "管理股票",
}

# 程序內快取：{symbol: StockInfo}
_COMPANY_MAP: Optional[Dict[str, StockInfo]] = None


def industry_label(code: str) -> str:
    """產業別代碼 → 中文；未知代碼回 f'產業{code}' (保持非空、可分群)。"""
    code = str(code or "").strip()
    if not code or code in ("－", "-", "—"):
        return ""
    return INDUSTRY_CODE_MAP.get(code.zfill(2), INDUSTRY_CODE_MAP.get(code, f"產業{code}"))


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9",
    })
    return s


def _date_to_iso(raw: str) -> str:
    """上市日期 → ISO yyyy-mm-dd。

    TWSE t187ap03_L 用 8 碼西元 (19940905)，TPEx DateOfListing 多為 7 碼民國
    (1140520)。無法解析回空字串。
    """
    s = str(raw or "").strip()
    if not s.isdigit():
        return ""
    if len(s) == 8:  # 西元 YYYYMMDD
        y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
    elif len(s) == 7:  # 民國 yyymmdd
        y, m, d = int(s[:3]) + 1911, int(s[3:5]), int(s[5:7])
    elif len(s) == 6:  # 民國 yymmdd
        y, m, d = int(s[:2]) + 1911, int(s[2:4]), int(s[4:6])
    else:
        return ""
    if not (1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"


def _map_path(root: Optional[Path]) -> Path:
    base = (root or Path.cwd()) / "data" / "meta"
    mk_folder(str(base))
    return base / "company_info.json"


def _parse_twse(rows: list) -> Dict[str, StockInfo]:
    out: Dict[str, StockInfo] = {}
    for r in rows or []:
        code = str(r.get("公司代號") or "").strip()
        if not code:
            continue
        out[code] = StockInfo(
            symbol=code,
            name=str(r.get("公司名稱") or "").strip(),
            short_name=str(r.get("公司簡稱") or "").strip(),
            market="TWSE",
            industry=industry_label(r.get("產業別") or ""),
            listed_date=_date_to_iso(r.get("上市日期") or ""),
        )
    return out


def _parse_tpex(rows: list) -> Dict[str, StockInfo]:
    out: Dict[str, StockInfo] = {}
    for r in rows or []:
        code = str(r.get("SecuritiesCompanyCode") or r.get("Symbol") or "").strip()
        if not code:
            continue
        out[code] = StockInfo(
            symbol=code,
            name=str(r.get("CompanyName") or "").strip(),
            short_name=str(r.get("CompanyAbbreviation") or "").strip(),
            market="TPEX",
            industry=industry_label(r.get("SecuritiesIndustryCode") or ""),
            listed_date=_date_to_iso(r.get("DateOfListing") or ""),
        )
    return out


def _build_company_map(
    *, session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, StockInfo]:
    log = logger or get_logger("company-info")
    sess = session or _session()
    out: Dict[str, StockInfo] = {}
    try:
        r = sess.get(URL_TWSE_COMPANY, timeout=30)
        if r.status_code == 200:
            out.update(_parse_twse(r.json()))
    except Exception:
        log.exception("讀取上市公司基本資料失敗")
    try:
        r = sess.get(URL_TPEX_COMPANY, timeout=30)
        if r.status_code == 200:
            for code, info in _parse_tpex(r.json()).items():
                out.setdefault(code, info)  # 上市優先，不覆蓋
    except Exception:
        log.exception("讀取上櫃公司基本資料失敗")
    return out


def load_company_map(
    *, root: Optional[Path] = None,
    force_refresh: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, StockInfo]:
    """載入 (或建立) 全市場公司基本資料對照表，每日快取一次。"""
    global _COMPANY_MAP
    if _COMPANY_MAP is not None and not force_refresh:
        return _COMPANY_MAP

    path = _map_path(root)
    today = now_tw().date().isoformat()
    if not force_refresh and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("date") == today and cached.get("map"):
                _COMPANY_MAP = {
                    str(k): StockInfo(**v) for k, v in cached["map"].items()
                }
                return _COMPANY_MAP
        except Exception:
            pass

    mp = _build_company_map(logger=logger)
    if mp:
        try:
            serializable = {
                k: {
                    "symbol": v.symbol, "name": v.name, "short_name": v.short_name,
                    "market": v.market, "industry": v.industry,
                    "listed_date": v.listed_date,
                }
                for k, v in mp.items()
            }
            path.write_text(
                json.dumps({"date": today, "map": serializable}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
        _COMPANY_MAP = mp
        return mp

    # 建表失敗 → 盡量用舊快取
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            _COMPANY_MAP = {str(k): StockInfo(**v) for k, v in (cached.get("map") or {}).items()}
            return _COMPANY_MAP
        except Exception:
            pass
    _COMPANY_MAP = {}
    return _COMPANY_MAP


def lookup_company_info(
    symbol: str, *, root: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[StockInfo]:
    """查單一代號的公司基本資料 (名稱/簡稱/產業/市場/上市日)。查無回 None。"""
    symbol = str(symbol).strip()
    if not symbol:
        return None
    mp = load_company_map(root=root, logger=logger)
    info = mp.get(symbol)
    if info is None:
        return None
    # 回傳複本，避免呼叫端就地修改快取
    return StockInfo(
        symbol=info.symbol, name=info.name, short_name=info.short_name,
        market=info.market, industry=info.industry, listed_date=info.listed_date,
    )


def backfill_stock_info(
    db, *, root: Optional[Path] = None,
    only_missing: bool = True,
    logger: Optional[logging.Logger] = None,
) -> int:
    """把全市場公司基本資料補進 stock_info。

    only_missing=True：只補「不存在」或「產業/名稱為空」的列，不覆蓋既有人工資料。
    回傳實際 upsert 的列數。
    """
    log = logger or get_logger("company-info")
    mp = load_company_map(root=root, logger=log)
    if not mp:
        log.warning("公司基本資料抓取為空，略過 backfill")
        return 0
    count = 0
    for symbol, fetched in mp.items():
        if only_missing:
            # 補缺漏：保留既有非空欄位 (人工資料優先)，只填補空的 (含 listed_date)。
            try:
                existing = db.get_stock_info(symbol)
            except Exception:
                existing = None
            merged = _merge(existing, fetched)
        else:
            merged = fetched  # 全覆寫：以官方資料為準
        try:
            db.upsert_stock_info(merged)
            count += 1
        except Exception:
            log.debug("upsert_stock_info 失敗: %s", symbol, exc_info=True)
    log.info("公司基本資料 backfill 完成：upsert %d 檔 (only_missing=%s)", count, only_missing)
    return count


def _merge(existing: Optional[StockInfo], fetched: StockInfo) -> StockInfo:
    """保留既有非空欄位 (人工資料優先)，缺的用官方資料補。"""
    if existing is None:
        return fetched
    return StockInfo(
        symbol=existing.symbol or fetched.symbol,
        name=existing.name or fetched.name,
        short_name=existing.short_name or fetched.short_name,
        market=existing.market or fetched.market,
        industry=existing.industry or fetched.industry,
        isin=existing.isin or fetched.isin,
        listed_date=existing.listed_date or fetched.listed_date,
        capital=existing.capital or fetched.capital,
        shares_outstanding=existing.shares_outstanding or fetched.shares_outstanding,
        cfi_code=existing.cfi_code or fetched.cfi_code,
        note=existing.note or fetched.note,
    )


__all__ = [
    "INDUSTRY_CODE_MAP",
    "URL_TWSE_COMPANY",
    "URL_TPEX_COMPANY",
    "backfill_stock_info",
    "industry_label",
    "load_company_map",
    "lookup_company_info",
]

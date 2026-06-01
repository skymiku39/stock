"""fundamentals_fetcher -- 從 TWSE / MOPS 公開資料抓基本面指標。

涵蓋面向（對應 Gemini 對話框架）：
* 每月營收 (含 YoY/MoM) -- TWSE OpenAPI `t187ap05_L`
* 每日 PER / PBR / 殖利率 -- TWSE OpenAPI `BWIBBU_ALL`
* 歷年股利政策 -- TWSE OpenAPI `t187ap46_L_dividend`
* EPS / 三率（毛利率、營業利益率、淨利率）-- MOPS 季報，
  另支援使用者匯入 CSV (data/fundamentals_manual/<ticker>.json)

設計
====
* 所有資料以日為單位或以年/月為單位快取在 `data/fundamentals/`。
* HTTP 失敗時靜默回傳空資料，呼叫端能用 `.has_data` 判斷。
* 不額外引入大型套件，僅依賴 requests + pandas。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from bot.cloud_file_cache import (
    read_json_cache as _read_cloud_json_cache,
    write_json_cache as _write_cloud_json_cache,
)
from bot.utils import get_logger, mk_folder, now_tw


# ----------------------------------------------------------------------
# Endpoint 集中表
# ----------------------------------------------------------------------

# 月營收 — TWSE OpenAPI 公開
URL_MONTHLY_REVENUE = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
# 月營收 — TPEx 上櫃
URL_TPEX_MONTHLY_REVENUE = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"

# 個股每日 PER / PBR / 殖利率
URL_BWIBBU_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
# 上櫃個股本益比 / 殖利率 / 股價淨值比
URL_TPEX_VALUATION = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"

# 個股股利分派 (年度)
URL_DIVIDEND = "https://openapi.twse.com.tw/v1/opendata/t187ap46_L_ex"

# 季報綜合損益表 (EPS、三率) — TWSE OpenAPI，依產業別分檔，免登入且穩定。
# 注意：TWSE 季報的損益數字為「年度累計」(Q2=上半年、Q3=前三季、Q4=全年)，
# 需自行 de-cumulate 成單季值。
QUARTERLY_ENDPOINTS = {
    "ci": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci",      # 一般業
    "ins": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ins",    # 保險業
    "basi": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_basi",  # 銀行業
    "bd": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_bd",      # 證券業
    "fh": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_fh",      # 金控業
    "mim": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_mim",    # 其他業
}

# 上櫃季報綜合損益表 (同樣分產業別)。注意：TPEx 端點的識別欄位常為英文鍵
# (SecuritiesCompanyCode / Year / Season)，解析時需同時相容中英文。
QUARTERLY_ENDPOINTS_TPEX = {
    "ci": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci",
    "ins": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ins",
    "basi": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_basi",
    "bd": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_bd",
    "fh": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_fh",
    "mim": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_mim",
}


# ----------------------------------------------------------------------
# 資料模型
# ----------------------------------------------------------------------


@dataclass
class MonthlyRevenue:
    """單月營收。"""

    ticker: str
    name: str = ""
    year: int = 0
    month: int = 0
    revenue: float = 0.0           # 當月營收（千元）
    revenue_last_year: float = 0.0  # 去年同期
    yoy: float = 0.0               # 年增率 %
    mom: float = 0.0               # 月增率 %
    cum_revenue: float = 0.0       # 累計營收
    cum_yoy: float = 0.0           # 累計年增率


@dataclass
class ValuationDaily:
    """個股當日 PER / PBR / 殖利率。"""

    ticker: str
    name: str = ""
    date: str = ""        # ISO 日期
    pe_ratio: float = 0.0
    pb_ratio: float = 0.0
    dividend_yield: float = 0.0      # %
    dividend_year: str = ""          # 殖利率所屬股利所屬年度


@dataclass
class DividendRecord:
    """單一年度股利分派。"""

    ticker: str
    year: int = 0                    # 股利所屬年度
    cash_dividend: float = 0.0       # 每股現金股利
    stock_dividend: float = 0.0      # 每股股票股利
    ex_dividend_date: str = ""       # 除息日
    ex_right_date: str = ""          # 除權日
    fill_date: str = ""              # 填息日
    fill_days: Optional[int] = None  # 填息所花天數
    payout_ratio: Optional[float] = None  # 盈餘分配率 %


@dataclass
class QuarterlyFinancials:
    """個股單季財報摘要 (來源優先 MOPS，失敗則手動匯入)。"""

    ticker: str
    year: int = 0
    quarter: int = 0                 # 1-4
    eps: float = 0.0
    gross_margin: float = 0.0        # %
    operating_margin: float = 0.0
    net_margin: float = 0.0
    revenue: float = 0.0
    operating_income: float = 0.0
    net_income: float = 0.0
    roe: Optional[float] = None
    note: str = ""


@dataclass
class FundamentalSnapshot:
    """整合單一個股的基本面快照。"""

    ticker: str
    name: str = ""
    fetched_at: str = ""
    valuation: Optional[ValuationDaily] = None
    revenues: List[MonthlyRevenue] = field(default_factory=list)
    dividends: List[DividendRecord] = field(default_factory=list)
    quarterlies: List[QuarterlyFinancials] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(
            self.valuation
            or self.revenues
            or self.dividends
            or self.quarterlies,
        )

    # --------- 摘要式衍生指標 ---------
    def latest_revenue(self) -> Optional[MonthlyRevenue]:
        if not self.revenues:
            return None
        return sorted(self.revenues, key=lambda r: (r.year, r.month))[-1]

    def revenue_yoy_streak(self) -> int:
        """連續幾個月 YoY > 0 (從最新月份向過去算)。"""
        sorted_rev = sorted(self.revenues, key=lambda r: (r.year, r.month), reverse=True)
        streak = 0
        for r in sorted_rev:
            if r.yoy > 0:
                streak += 1
            else:
                break
        return streak

    def avg_payout_ratio(self, lookback: int = 5) -> Optional[float]:
        ratios = [d.payout_ratio for d in self.dividends if d.payout_ratio is not None]
        ratios = ratios[-lookback:]
        if not ratios:
            return None
        return sum(ratios) / len(ratios)

    def rolling_eps_progress(self) -> Dict[str, Any]:
        """以目前已公布的最新年度，回傳「該年度逐季累計 EPS」進度。"""
        if not self.quarterlies:
            return {}
        latest_year = max(q.year for q in self.quarterlies)
        items = sorted(
            [q for q in self.quarterlies if q.year == latest_year],
            key=lambda q: q.quarter,
        )
        cum = 0.0
        progress = []
        for q in items:
            cum += q.eps
            progress.append({
                "label": f"Q{q.quarter}",
                "eps": q.eps,
                "cum_eps": round(cum, 2),
            })
        prev_year_items = sorted(
            [q for q in self.quarterlies if q.year == latest_year - 1],
            key=lambda q: q.quarter,
        )
        prev_total = round(sum(q.eps for q in prev_year_items), 2)
        return {
            "year": latest_year,
            "progress": progress,
            "prev_year_total_eps": prev_total,
        }


# ----------------------------------------------------------------------
# HTTP / 快取
# ----------------------------------------------------------------------


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9",
    })
    return s


def _cache_root(root: Optional[Path] = None) -> Path:
    base = (root or Path.cwd()) / "data" / "fundamentals"
    mk_folder(str(base))
    return base


def _read_json_cache(path: Path, ttl_seconds: Optional[int] = None) -> Optional[Any]:
    return _read_cloud_json_cache(path, ttl_seconds=ttl_seconds)


def _write_json_cache(path: Path, data: Any) -> None:
    _write_cloud_json_cache(path, data, indent=2)


def _to_float(x: Any) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").replace("--", "").strip()
    if not s or s in ("-", "N/A"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ----------------------------------------------------------------------
# 公開 API：月營收
# ----------------------------------------------------------------------


def fetch_monthly_revenue_all(
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    use_cache: bool = True,
    cache_ttl: int = 6 * 3600,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """抓 TWSE OpenAPI 全市場最新月營收 (回傳原始 list[dict])。"""
    log = logger or get_logger("fundamentals")
    sess = session or _session()
    cache = _cache_root(root) / "monthly_revenue_all.json"
    if use_cache:
        cached = _read_json_cache(cache, ttl_seconds=cache_ttl)
        if cached is not None:
            return cached
    combined: List[Dict[str, Any]] = []
    for label, url in (("上市", URL_MONTHLY_REVENUE), ("上櫃", URL_TPEX_MONTHLY_REVENUE)):
        try:
            resp = sess.get(url, timeout=20)
            if resp.status_code != 200:
                log.warning("月營收(%s) HTTP %d", label, resp.status_code)
                continue
            data = resp.json()
            if isinstance(data, list):
                combined.extend(data)
        except Exception:
            log.exception("月營收(%s)抓取失敗", label)
    if combined:
        _write_json_cache(cache, combined)
    return combined


def _parse_one_revenue(raw: Dict[str, Any]) -> Optional[MonthlyRevenue]:
    """欄位名稱會偶爾調整；採容錯抽取。"""
    if not isinstance(raw, dict):
        return None
    code_keys = ["公司代號", "公司代碼", "證券代號", "stock_code"]
    name_keys = ["公司名稱", "證券名稱", "company_name"]
    year_keys = ["資料年月", "年月", "datatime"]
    rev_keys = ["營業收入-當月營收", "當月營收", "revenue"]
    last_year_keys = ["營業收入-去年當月營收", "去年當月營收"]
    yoy_keys = ["營業收入-去年同月增減(%)", "去年同月增減(%)"]
    mom_keys = ["營業收入-上月比較增減(%)", "上月比較增減(%)"]
    cum_keys = ["營業收入-當月累計營收", "累計營業收入-當月累計營收", "當月累計營收"]
    cum_yoy_keys = ["營業收入-前期比較增減(%)", "累計營業收入-前期比較增減(%)", "前期比較增減(%)"]

    def pick(keys: List[str]) -> Any:
        for k in keys:
            if k in raw:
                return raw[k]
        return None

    code = str(pick(code_keys) or "").strip()
    if not code:
        return None
    yyyymm = str(pick(year_keys) or "").strip()
    year = 0
    month = 0
    if yyyymm and yyyymm.isdigit() and len(yyyymm) >= 4:
        try:
            year = int(yyyymm[:-2])
            if year < 1911:
                year += 1911
            month = int(yyyymm[-2:])
        except ValueError:
            pass
    return MonthlyRevenue(
        ticker=code,
        name=str(pick(name_keys) or ""),
        year=year,
        month=month,
        revenue=_to_float(pick(rev_keys)),
        revenue_last_year=_to_float(pick(last_year_keys)),
        yoy=_to_float(pick(yoy_keys)),
        mom=_to_float(pick(mom_keys)),
        cum_revenue=_to_float(pick(cum_keys)),
        cum_yoy=_to_float(pick(cum_yoy_keys)),
    )


def fetch_monthly_revenue(
    ticker: str,
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> List[MonthlyRevenue]:
    """傳回個股已快取的所有月營收紀錄 (依檔案內 manual 累積)。

    流程：
    1. 從 `monthly_revenue_all.json` (TWSE 全市場) 抓最新一筆。
    2. 從 `data/fundamentals/<ticker>/monthly_revenue.json` 載入歷史，
       將新一筆合併 (相同年月會覆寫)，再寫回。
    3. 回傳依年月排序的清單。
    """
    log = logger or get_logger("fundamentals")
    all_rev = fetch_monthly_revenue_all(
        root=root, session=session, logger=log,
    )
    matched = None
    for raw in all_rev:
        try:
            mr = _parse_one_revenue(raw)
        except Exception:
            continue
        if mr and mr.ticker == ticker:
            matched = mr
            break

    ticker_dir = _cache_root(root) / ticker
    mk_folder(str(ticker_dir))
    history_path = ticker_dir / "monthly_revenue.json"
    history: List[Dict[str, Any]] = []
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except Exception:
            history = []

    if matched:
        key = (matched.year, matched.month)
        history = [h for h in history if (h.get("year"), h.get("month")) != key]
        history.append(asdict(matched))
        history.sort(key=lambda r: (r.get("year", 0), r.get("month", 0)))
        _write_json_cache(history_path, history)

    out: List[MonthlyRevenue] = []
    for h in history:
        try:
            out.append(MonthlyRevenue(**h))
        except TypeError:
            continue
    return out


# ----------------------------------------------------------------------
# 公開 API：PER / PBR / 殖利率
# ----------------------------------------------------------------------


def fetch_valuation_all(
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    use_cache: bool = True,
    cache_ttl: int = 6 * 3600,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """抓 TWSE OpenAPI 全市場最新 PER/PBR/殖利率 (回傳原始 list[dict])。"""
    log = logger or get_logger("fundamentals")
    sess = session or _session()
    cache = _cache_root(root) / "valuation_all.json"
    if use_cache:
        cached = _read_json_cache(cache, ttl_seconds=cache_ttl)
        if cached is not None:
            return cached
    combined: List[Dict[str, Any]] = []
    # 上市 BWIBBU
    try:
        resp = sess.get(URL_BWIBBU_ALL, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                combined.extend(data)
        else:
            log.warning("Valuation(上市) HTTP %d", resp.status_code)
    except Exception:
        log.exception("Valuation(上市)抓取失敗")
    # 上櫃 — 正規化成與 BWIBBU 相容的欄位
    try:
        resp = sess.get(URL_TPEX_VALUATION, timeout=20)
        if resp.status_code == 200:
            for row in resp.json() or []:
                combined.append({
                    "Code": row.get("SecuritiesCompanyCode"),
                    "Name": row.get("CompanyName"),
                    "PEratio": row.get("PriceEarningRatio"),
                    "PBratio": row.get("PriceBookRatio"),
                    "DividendYield": row.get("YieldRatio"),
                })
        else:
            log.warning("Valuation(上櫃) HTTP %d", resp.status_code)
    except Exception:
        log.exception("Valuation(上櫃)抓取失敗")
    if combined:
        _write_json_cache(cache, combined)
    return combined


def _parse_valuation(raw: Dict[str, Any]) -> Optional[ValuationDaily]:
    if not isinstance(raw, dict):
        return None
    code = str(raw.get("Code") or raw.get("證券代號") or "").strip()
    if not code:
        return None
    return ValuationDaily(
        ticker=code,
        name=str(raw.get("Name") or raw.get("證券名稱") or ""),
        date=now_tw().date().isoformat(),
        pe_ratio=_to_float(raw.get("PEratio") or raw.get("本益比")),
        pb_ratio=_to_float(raw.get("PBratio") or raw.get("股價淨值比")),
        dividend_yield=_to_float(raw.get("DividendYield") or raw.get("殖利率(%)")),
        dividend_year=str(raw.get("FinancialYear") or raw.get("股利年度") or ""),
    )


def fetch_valuation(
    ticker: str,
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[ValuationDaily]:
    """個股當日估值。"""
    rows = fetch_valuation_all(root=root, session=session, logger=logger)
    for raw in rows:
        v = _parse_valuation(raw)
        if v and v.ticker == ticker:
            ticker_dir = _cache_root(root) / ticker
            mk_folder(str(ticker_dir))
            _write_json_cache(ticker_dir / "valuation_latest.json", asdict(v))
            return v
    cached = _read_json_cache(_cache_root(root) / ticker / "valuation_latest.json")
    if cached:
        try:
            return ValuationDaily(**cached)
        except TypeError:
            return None
    return None


# ----------------------------------------------------------------------
# 公開 API：股利
# ----------------------------------------------------------------------


def fetch_dividend_all(
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    use_cache: bool = True,
    cache_ttl: int = 24 * 3600,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    log = logger or get_logger("fundamentals")
    sess = session or _session()
    cache = _cache_root(root) / "dividends_all.json"
    if use_cache:
        cached = _read_json_cache(cache, ttl_seconds=cache_ttl)
        if cached is not None:
            return cached
    try:
        resp = sess.get(URL_DIVIDEND, timeout=20)
        if resp.status_code != 200:
            log.warning("Dividend HTTP %d", resp.status_code)
            return []
        data = resp.json()
        if isinstance(data, list):
            _write_json_cache(cache, data)
            return data
    except Exception:
        log.exception("Dividend 抓取失敗")
    return []


def _parse_dividend(raw: Dict[str, Any], ticker: str) -> Optional[DividendRecord]:
    code = str(raw.get("Code") or raw.get("股票代號") or raw.get("公司代號") or "").strip()
    if code != ticker:
        return None
    year_str = str(raw.get("Year") or raw.get("年度") or raw.get("股利年度") or "").strip()
    try:
        year = int(year_str)
        if year < 1911:
            year += 1911
    except ValueError:
        year = 0
    cash = _to_float(raw.get("CashEarningsDistribution") or raw.get("現金股利"))
    stock = _to_float(raw.get("StockEarningsDistribution") or raw.get("股票股利"))
    return DividendRecord(
        ticker=code,
        year=year,
        cash_dividend=cash,
        stock_dividend=stock,
        ex_dividend_date=str(raw.get("CashExDividendTradingDate") or raw.get("除息日") or ""),
        ex_right_date=str(raw.get("StockExDividendTradingDate") or raw.get("除權日") or ""),
        fill_date=str(raw.get("FillDate") or ""),
    )


def fetch_dividends(
    ticker: str,
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> List[DividendRecord]:
    """個股歷年股利紀錄；TWSE OpenAPI 主要回最新一筆，舊年度需 manual 累積。"""
    log = logger or get_logger("fundamentals")
    rows = fetch_dividend_all(root=root, session=session, logger=log)
    new_records: List[DividendRecord] = []
    for raw in rows:
        rec = _parse_dividend(raw, ticker)
        if rec:
            new_records.append(rec)

    ticker_dir = _cache_root(root) / ticker
    mk_folder(str(ticker_dir))
    history_path = ticker_dir / "dividends.json"
    history: List[Dict[str, Any]] = []
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except Exception:
            history = []

    for r in new_records:
        history = [h for h in history if h.get("year") != r.year]
        history.append(asdict(r))
    history.sort(key=lambda x: x.get("year", 0))
    _write_json_cache(history_path, history)

    out: List[DividendRecord] = []
    for h in history:
        try:
            out.append(DividendRecord(**h))
        except TypeError:
            continue
    return out


# ----------------------------------------------------------------------
# 公開 API：季報 (EPS / 三率)
# ----------------------------------------------------------------------


def load_manual_quarterlies(
    ticker: str,
    *,
    root: Optional[Path] = None,
) -> List[QuarterlyFinancials]:
    """從 data/fundamentals_manual/<ticker>.json 載入使用者手動匯入的季報。

    手動匯入 JSON 格式：
        {
          "ticker": "2330",
          "quarterlies": [
            {"year": 2025, "quarter": 3, "eps": 14.71, "gross_margin": 59.1, ...},
            ...
          ]
        }

    這設計讓我們在沒有付費 API 的狀況下，仍能讓 Q1~Q4 滾動 EPS、三率走勢圖
    等功能可用。
    """
    base = (root or Path.cwd()) / "data" / "fundamentals_manual"
    path = base / f"{ticker}.json"
    raw = _read_json_cache(path)
    if not raw:
        return []
    items = raw.get("quarterlies", [])
    out: List[QuarterlyFinancials] = []
    for it in items:
        try:
            out.append(QuarterlyFinancials(
                ticker=ticker,
                year=int(it.get("year", 0)),
                quarter=int(it.get("quarter", 0)),
                eps=_to_float(it.get("eps")),
                gross_margin=_to_float(it.get("gross_margin")),
                operating_margin=_to_float(it.get("operating_margin")),
                net_margin=_to_float(it.get("net_margin")),
                revenue=_to_float(it.get("revenue")),
                operating_income=_to_float(it.get("operating_income")),
                net_income=_to_float(it.get("net_income")),
                roe=_to_float(it.get("roe")) if it.get("roe") is not None else None,
                note=str(it.get("note", "")),
            ))
        except Exception:
            continue
    return out


def save_manual_quarterlies(
    ticker: str,
    quarterlies: List[QuarterlyFinancials],
    *,
    root: Optional[Path] = None,
) -> Path:
    base = (root or Path.cwd()) / "data" / "fundamentals_manual"
    mk_folder(str(base))
    path = base / f"{ticker}.json"
    payload = {
        "ticker": ticker,
        "updated_at": now_tw().isoformat(timespec="seconds"),
        "quarterlies": [asdict(q) for q in quarterlies],
    }
    _write_cloud_json_cache(path, payload, indent=2)
    return path


# ----------------------------------------------------------------------
# 公開 API：季報 EPS / 三率 (TWSE 綜合損益表 OpenAPI)
# ----------------------------------------------------------------------


def fetch_quarterly_financials_all(
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    use_cache: bool = True,
    cache_ttl: int = 6 * 3600,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """抓 TWSE 全市場「最近一季」綜合損益表 (合併各產業別 endpoint)。

    回傳原始 list[dict]，每筆額外帶一個 `_sector` 標記其來源產業別。
    """
    log = logger or get_logger("fundamentals")
    sess = session or _session()
    cache = _cache_root(root) / "quarterly_financials_all.json"
    if use_cache:
        cached = _read_json_cache(cache, ttl_seconds=cache_ttl)
        if cached is not None:
            return cached

    combined: List[Dict[str, Any]] = []
    endpoints = [(m, s, u) for m, mp in (("twse", QUARTERLY_ENDPOINTS), ("tpex", QUARTERLY_ENDPOINTS_TPEX))
                 for s, u in mp.items()]
    for market, sector, url in endpoints:
        try:
            resp = sess.get(url, timeout=20)
            if resp.status_code != 200:
                log.warning("季報 %s/%s HTTP %d", market, sector, resp.status_code)
                continue
            data = resp.json()
            if isinstance(data, list):
                for row in data:
                    if isinstance(row, dict):
                        row = dict(row)
                        row["_sector"] = sector
                        row["_market"] = market
                        combined.append(row)
        except Exception:
            log.exception("季報 %s/%s 抓取失敗", market, sector)

    if combined:
        _write_json_cache(cache, combined)
    return combined


def _pick_key(raw: Dict[str, Any], includes: List[str], excludes: Optional[List[str]] = None) -> Any:
    """回傳第一個 key 同時包含 includes 任一關鍵字、且不含 excludes 任一關鍵字的值。"""
    excludes = excludes or []
    for k in raw.keys():
        if any(inc in k for inc in includes) and not any(exc in k for exc in excludes):
            return raw[k]
    return None


def _parse_quarterly_raw(raw: Dict[str, Any], ticker: str) -> Optional[Dict[str, Any]]:
    """把一筆綜合損益表轉成累計 (cumulative) 原始值 dict。

    識別欄位同時相容上市 (公司代號/年度/季別) 與上櫃 (SecuritiesCompanyCode/Year/Season)。
    """
    code = str(raw.get("公司代號") or raw.get("SecuritiesCompanyCode") or "").strip()
    if code != ticker:
        return None
    year_str = str(raw.get("年度") or raw.get("Year") or "").strip()
    quarter_str = str(raw.get("季別") or raw.get("Season") or "").strip()
    try:
        year = int(year_str)
        if year < 1911:  # 民國年 → 西元年
            year += 1911
        quarter = int(quarter_str)
    except ValueError:
        return None
    if quarter < 1 or quarter > 4:
        return None

    # 營業收入只在「一般業 / 保險業」有意義；金融、證券、金控以淨收益概念呈現，
    # 為避免毛利率/營益率誤導，僅在抓得到 "營業收入" 時計算三率。
    revenue = _to_float(_pick_key(raw, ["營業收入"], excludes=["外"]))
    gross = _to_float(_pick_key(raw, ["營業毛利"]))
    op_income = _to_float(_pick_key(raw, ["營業利益"]))
    # 本期淨利：排除「繼續營業單位」「綜合損益」「歸屬」等變體
    net_income = _to_float(
        _pick_key(raw, ["本期淨利", "本期稅後淨利"], excludes=["繼續", "綜合", "歸屬"])
    )
    eps = _to_float(_pick_key(raw, ["每股盈餘"]))

    return {
        "year": year,
        "quarter": quarter,
        "sector": str(raw.get("_sector") or ""),
        "cum_revenue": revenue,
        "cum_gross": gross,
        "cum_op_income": op_income,
        "cum_net_income": net_income,
        "cum_eps": eps,
    }


def _decumulate_quarterlies(
    ticker: str,
    raw_records: List[Dict[str, Any]],
) -> List[QuarterlyFinancials]:
    """把年度累計的季報轉成「單季」QuarterlyFinancials，並計算三率。

    台股季報損益為年度累計：Q2=上半年、Q3=前三季、Q4=全年。
    單季值 = 本季累計 - 上一季累計 (同年度且上一季存在時)；Q1 直接採用。
    """
    by_year: Dict[int, Dict[int, Dict[str, Any]]] = {}
    for r in raw_records:
        by_year.setdefault(int(r["year"]), {})[int(r["quarter"])] = r

    out: List[QuarterlyFinancials] = []
    for year, quarters in by_year.items():
        for q, rec in quarters.items():
            prev = quarters.get(q - 1) if q > 1 else None
            if prev is not None:
                rev = rec["cum_revenue"] - prev["cum_revenue"]
                gross = rec["cum_gross"] - prev["cum_gross"]
                op_income = rec["cum_op_income"] - prev["cum_op_income"]
                net_income = rec["cum_net_income"] - prev["cum_net_income"]
                eps = round(rec["cum_eps"] - prev["cum_eps"], 2)
                note = "公開資料自動 (單季)"
            else:
                # 上一季累計缺漏：Q1 為單季；Q2~Q4 缺前季時只能先用累計值
                rev = rec["cum_revenue"]
                gross = rec["cum_gross"]
                op_income = rec["cum_op_income"]
                net_income = rec["cum_net_income"]
                eps = round(rec["cum_eps"], 2)
                note = "公開資料自動 (單季)" if q == 1 else "公開資料自動 (累計，缺前季)"

            gross_margin = round(100.0 * gross / rev, 2) if rev > 0 else 0.0
            operating_margin = round(100.0 * op_income / rev, 2) if rev > 0 else 0.0
            net_margin = round(100.0 * net_income / rev, 2) if rev > 0 else 0.0

            out.append(QuarterlyFinancials(
                ticker=ticker,
                year=year,
                quarter=q,
                eps=eps,
                gross_margin=gross_margin,
                operating_margin=operating_margin,
                net_margin=net_margin,
                revenue=rev,
                operating_income=op_income,
                net_income=net_income,
                note=note,
            ))
    out.sort(key=lambda x: (x.year, x.quarter))
    return out


def fetch_quarterly_financials(
    ticker: str,
    *,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> List[QuarterlyFinancials]:
    """個股季報 EPS / 三率。

    TWSE OpenAPI 只提供「最近一季」全市場資料，因此本函式採滾動累積：
    把每次抓到的最新一季累計值合併到 `data/fundamentals/<ticker>/quarterlies_raw.json`，
    再 de-cumulate 成單季 QuarterlyFinancials 回傳。
    """
    log = logger or get_logger("fundamentals")
    rows = fetch_quarterly_financials_all(root=root, session=session, logger=log)

    new_raw: List[Dict[str, Any]] = []
    for raw in rows:
        try:
            parsed = _parse_quarterly_raw(raw, ticker)
        except Exception:
            continue
        if parsed:
            new_raw.append(parsed)

    ticker_dir = _cache_root(root) / ticker
    mk_folder(str(ticker_dir))
    raw_path = ticker_dir / "quarterlies_raw.json"
    history: List[Dict[str, Any]] = []
    if raw_path.exists():
        try:
            history = json.loads(raw_path.read_text(encoding="utf-8"))
        except Exception:
            history = []

    for rec in new_raw:
        key = (rec["year"], rec["quarter"])
        history = [h for h in history if (h.get("year"), h.get("quarter")) != key]
        history.append(rec)
    history.sort(key=lambda x: (x.get("year", 0), x.get("quarter", 0)))
    if new_raw:
        _write_json_cache(raw_path, history)

    return _decumulate_quarterlies(ticker, history)


# ----------------------------------------------------------------------
# 整合 Snapshot
# ----------------------------------------------------------------------


def build_fundamental_snapshot(
    ticker: str,
    *,
    name_hint: str = "",
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
    refresh: bool = True,
) -> FundamentalSnapshot:
    """組合單一個股的基本面 snapshot。

    Args:
        refresh: True 會打 TWSE OpenAPI 更新最新月營收/估值/股利。
                 False 只讀本地 cache。
    """
    log = logger or get_logger("fundamentals")
    sess = session or _session() if refresh else None
    revs: List[MonthlyRevenue] = []
    val: Optional[ValuationDaily] = None
    divs: List[DividendRecord] = []
    auto_quarterlies: List[QuarterlyFinancials] = []
    name = name_hint

    if refresh:
        try:
            revs = fetch_monthly_revenue(ticker, root=root, session=sess, logger=log)
        except Exception:
            log.exception("[%s] monthly revenue refresh 失敗", ticker)
        try:
            val = fetch_valuation(ticker, root=root, session=sess, logger=log)
        except Exception:
            log.exception("[%s] valuation refresh 失敗", ticker)
        try:
            divs = fetch_dividends(ticker, root=root, session=sess, logger=log)
        except Exception:
            log.exception("[%s] dividends refresh 失敗", ticker)
        try:
            auto_quarterlies = fetch_quarterly_financials(ticker, root=root, session=sess, logger=log)
        except Exception:
            log.exception("[%s] 季報 refresh 失敗", ticker)
    else:
        ticker_dir = _cache_root(root) / ticker
        if ticker_dir.exists():
            for h in _read_json_cache(ticker_dir / "monthly_revenue.json") or []:
                try:
                    revs.append(MonthlyRevenue(**h))
                except TypeError:
                    continue
            cached_val = _read_json_cache(ticker_dir / "valuation_latest.json")
            if cached_val:
                try:
                    val = ValuationDaily(**cached_val)
                except TypeError:
                    pass
            for h in _read_json_cache(ticker_dir / "dividends.json") or []:
                try:
                    divs.append(DividendRecord(**h))
                except TypeError:
                    continue
            raw_q = _read_json_cache(ticker_dir / "quarterlies_raw.json") or []
            if raw_q:
                try:
                    auto_quarterlies = _decumulate_quarterlies(ticker, raw_q)
                except Exception:
                    auto_quarterlies = []

    # 合併季報：自動抓取為底，使用者手動匯入優先 (覆蓋同年同季)
    manual_quarterlies = load_manual_quarterlies(ticker, root=root)
    quarterlies = _merge_quarterlies(auto_quarterlies, manual_quarterlies)

    if val and val.name:
        name = name or val.name
    if revs and not name:
        name = revs[-1].name

    divs = _enrich_dividends_with_payout(divs, quarterlies)
    divs = _enrich_dividends_with_fill_days(ticker, divs, root=root, logger=log)

    return FundamentalSnapshot(
        ticker=ticker,
        name=name,
        fetched_at=now_tw().isoformat(timespec="seconds"),
        valuation=val,
        revenues=revs,
        dividends=divs,
        quarterlies=quarterlies,
    )


def _merge_quarterlies(
    auto: List[QuarterlyFinancials],
    manual: List[QuarterlyFinancials],
) -> List[QuarterlyFinancials]:
    """合併自動抓取與手動匯入的季報；相同 (年, 季) 以手動匯入為準。"""
    merged: Dict[tuple, QuarterlyFinancials] = {}
    for q in auto:
        merged[(q.year, q.quarter)] = q
    for q in manual:
        merged[(q.year, q.quarter)] = q
    return sorted(merged.values(), key=lambda x: (x.year, x.quarter))


def _enrich_dividends_with_payout(
    divs: List[DividendRecord],
    quarterlies: List[QuarterlyFinancials],
) -> List[DividendRecord]:
    """以該年度 4 季 EPS 估算 payout_ratio (簡易版)。"""
    if not divs:
        return divs
    eps_by_year: Dict[int, float] = {}
    for q in quarterlies:
        eps_by_year[q.year] = eps_by_year.get(q.year, 0.0) + q.eps
    for d in divs:
        eps = eps_by_year.get(d.year)
        if eps and eps > 0:
            d.payout_ratio = round(100.0 * (d.cash_dividend + d.stock_dividend) / eps, 1)
    return divs


def _enrich_dividends_with_fill_days(
    ticker: str,
    divs: List[DividendRecord],
    *,
    root: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
) -> List[DividendRecord]:
    """以快取日 K 計算各除息年度的「填息日」與「填息天數」。

    定義：除息日後第一個「收盤價 >= 除息前一交易日收盤價」的交易日即填息完成；
    填息天數 = 該日與除息日之間的「交易日」數。

    僅使用本地已快取的 K 線 (DB / CSV)，缺資料則保持原值 (不額外打網路)。
    """
    if not divs:
        return divs
    targets = [
        d for d in divs
        if d.ex_dividend_date and d.fill_days is None
    ]
    if not targets:
        return divs

    try:
        from bot.technicals import load_kline_from_db, get_kline_coverage
    except Exception:
        return divs

    cov = None
    try:
        cov = get_kline_coverage(ticker, root=root)
    except Exception:
        cov = None
    if not cov:
        return divs

    for d in targets:
        ex_date = _parse_iso_date(d.ex_dividend_date)
        if ex_date is None:
            continue
        # 取除息日前後約 250 交易日 (約一年) 的 K 線
        start = (ex_date - dt.timedelta(days=10)).isoformat()
        end = (ex_date + dt.timedelta(days=400)).isoformat()
        try:
            df = load_kline_from_db(ticker, start=start, end=end, root=root)
        except Exception:
            df = None
        if df is None or getattr(df, "empty", True):
            continue
        try:
            rows = [
                (str(r["date"]), float(r["close"]))
                for _, r in df.sort_values("date").iterrows()
                if str(r.get("date"))
            ]
        except Exception:
            continue
        ex_iso = ex_date.isoformat()
        before = [c for (dte, c) in rows if dte < ex_iso and c > 0]
        if not before:
            continue
        pre_close = before[-1]
        after = [(dte, c) for (dte, c) in rows if dte >= ex_iso]
        if not after:
            continue
        # after[0] 為除息日當天 (交易日基準)
        fill_idx = None
        for i, (dte, c) in enumerate(after):
            if i == 0:
                continue  # 除息日當天不算填息
            if c >= pre_close:
                fill_idx = i
                d.fill_date = dte
                break
        if fill_idx is not None:
            d.fill_days = fill_idx  # 除息後第幾個交易日填息
    return divs


def _parse_iso_date(s: str) -> Optional[dt.date]:
    """容錯解析日期：支援 'YYYY-MM-DD'、'YYYYMMDD'、民國 'YYYMMDD'。"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        if "-" in s:
            return dt.date.fromisoformat(s[:10])
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) == 8:  # 西元 YYYYMMDD
            return dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        if len(digits) == 7:  # 民國 YYYMMDD
            return dt.date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7]))
    except ValueError:
        return None
    return None


def snapshot_to_dict(s: FundamentalSnapshot) -> Dict[str, Any]:
    return {
        "ticker": s.ticker,
        "name": s.name,
        "fetched_at": s.fetched_at,
        "valuation": asdict(s.valuation) if s.valuation else None,
        "revenues": [asdict(r) for r in s.revenues],
        "dividends": [asdict(d) for d in s.dividends],
        "quarterlies": [asdict(q) for q in s.quarterlies],
        "derived": {
            "latest_revenue": (
                asdict(s.latest_revenue()) if s.latest_revenue() else None
            ),
            "revenue_yoy_streak": s.revenue_yoy_streak(),
            "avg_payout_ratio": s.avg_payout_ratio(),
            "rolling_eps_progress": s.rolling_eps_progress(),
        },
    }


__all__ = [
    "DividendRecord",
    "FundamentalSnapshot",
    "MonthlyRevenue",
    "QuarterlyFinancials",
    "ValuationDaily",
    "build_fundamental_snapshot",
    "fetch_dividend_all",
    "fetch_dividends",
    "fetch_monthly_revenue",
    "fetch_monthly_revenue_all",
    "fetch_quarterly_financials",
    "fetch_quarterly_financials_all",
    "fetch_valuation",
    "fetch_valuation_all",
    "load_manual_quarterlies",
    "save_manual_quarterlies",
    "snapshot_to_dict",
]

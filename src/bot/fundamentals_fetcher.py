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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from bot.utils import get_logger, mk_folder, now_tw


# ----------------------------------------------------------------------
# Endpoint 集中表
# ----------------------------------------------------------------------

# 月營收 — TWSE OpenAPI 公開
URL_MONTHLY_REVENUE = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"

# 個股每日 PER / PBR / 殖利率
URL_BWIBBU_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"

# 個股股利分派 (年度)
URL_DIVIDEND = "https://openapi.twse.com.tw/v1/opendata/t187ap46_L_ex"

# MOPS 個股財報 (EPS、三率) — 簡易 ajax 介面
URL_MOPS_T164SB04 = "https://mops.twse.com.tw/mops/web/ajax_t164sb04"


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
    if not path.exists():
        return None
    if ttl_seconds is not None:
        age = time.time() - path.stat().st_mtime
        if age > ttl_seconds:
            return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json_cache(path: Path, data: Any) -> None:
    mk_folder(str(path.parent))
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
    try:
        resp = sess.get(URL_MONTHLY_REVENUE, timeout=20)
        if resp.status_code != 200:
            log.warning("月營收 HTTP %d", resp.status_code)
            return []
        data = resp.json()
        if isinstance(data, list):
            _write_json_cache(cache, data)
            return data
    except Exception:
        log.exception("月營收抓取失敗")
    return []


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
    cum_keys = ["營業收入-當月累計營收", "當月累計營收"]
    cum_yoy_keys = ["營業收入-前期比較增減(%)", "前期比較增減(%)"]

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
    try:
        resp = sess.get(URL_BWIBBU_ALL, timeout=20)
        if resp.status_code != 200:
            log.warning("Valuation HTTP %d", resp.status_code)
            return []
        data = resp.json()
        if isinstance(data, list):
            _write_json_cache(cache, data)
            return data
    except Exception:
        log.exception("Valuation 抓取失敗")
    return []


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
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


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

    quarterlies = load_manual_quarterlies(ticker, root=root)

    if val and val.name:
        name = name or val.name
    if revs and not name:
        name = revs[-1].name

    divs = _enrich_dividends_with_payout(divs, quarterlies)

    return FundamentalSnapshot(
        ticker=ticker,
        name=name,
        fetched_at=now_tw().isoformat(timespec="seconds"),
        valuation=val,
        revenues=revs,
        dividends=divs,
        quarterlies=quarterlies,
    )


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
    "fetch_valuation",
    "fetch_valuation_all",
    "load_manual_quarterlies",
    "save_manual_quarterlies",
    "snapshot_to_dict",
]

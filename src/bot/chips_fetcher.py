"""chips_fetcher -- 自動從 TWSE 公開 API 抓籌碼面資料。

涵蓋：
* 三大法人買賣超 (T86 / 上市)
* 借券賣出餘額 (TWT93U)
* 融資融券餘額 (MI_MARGN)
* 鉅額交易 (BFT41U)

所有資料以日為單位快取於 data/chips/<date>/<endpoint>.json，
重複呼叫不會打爆 TWSE，缺檔才會 HTTP fetch。
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

# 採用較穩定的 rwd 路徑；若 TWSE 端點異動，可在這裡集中替換。
ENDPOINTS = {
    # 三大法人 (上市) — 每檔個股的外資/投信/自營商買賣超
    "qfii_daily": (
        "https://www.twse.com.tw/rwd/zh/fund/T86"
        "?date={date_compact}&selectType=ALLBUT0999&response=json"
    ),
    # 借券賣出餘額
    "borrow_balance": (
        "https://www.twse.com.tw/rwd/zh/SBL/TWT93U?response=json&date={date_compact}"
    ),
    # 融資融券餘額
    "margin_balance": (
        "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
        "?date={date_compact}&selectType=ALL&response=json"
    ),
    # 鉅額交易
    "block_trade": (
        "https://www.twse.com.tw/rwd/zh/block/BFIAUU"
        "?date={date_compact}&selectType=S&response=json"
    ),
}


# ----------------------------------------------------------------------
# 資料模型
# ----------------------------------------------------------------------


@dataclass
class ChipDailyRow:
    """個股單日籌碼摘要。"""

    ticker: str
    name: str = ""
    foreign_net: float = 0.0          # 外資 + 陸資 淨買賣超 (張)
    investment_trust_net: float = 0.0
    dealer_net: float = 0.0
    margin_balance: float = 0.0       # 融資餘額 (張)
    short_balance: float = 0.0        # 融券餘額 (張)
    borrow_balance: float = 0.0       # 借券賣出餘額 (張)


@dataclass
class ChipSummary:
    """個股近 N 日的籌碼面累計摘要 (供 logic_check 用)。"""

    ticker: str
    days: int = 5
    foreign_net: float = 0.0
    investment_trust_net: float = 0.0
    dealer_net: float = 0.0
    margin_buy_change_pct: float = 0.0
    short_borrow_change_pct: float = 0.0
    block_trade_net: float = 0.0
    rows: List[ChipDailyRow] = field(default_factory=list)


# ----------------------------------------------------------------------
# 快取 IO
# ----------------------------------------------------------------------


def _cache_path(name: str, date: dt.date, root: Optional[Path] = None) -> Path:
    base = root or Path.cwd()
    return base / "data" / "chips" / date.isoformat() / f"{name}.json"


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Accept": "application/json, text/plain, */*",
    })
    return s


def _fetch_endpoint(
    name: str,
    date: dt.date,
    *,
    session: Optional[requests.Session] = None,
    root: Optional[Path] = None,
    use_cache: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    log = logger or get_logger("chips")
    sess = session or _new_session()
    path = _cache_path(name, date, root)
    if use_cache and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    url = ENDPOINTS[name].format(date_compact=date.strftime("%Y%m%d"))
    try:
        resp = sess.get(url, timeout=20)
        if resp.status_code != 200:
            log.warning("%s HTTP %d (%s)", name, resp.status_code, url)
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        mk_folder(str(path.parent))
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        time.sleep(0.5)  # 對 TWSE 友善
        return data
    except Exception:
        log.exception("%s 抓取失敗 (%s)", name, url)
        return None


# ----------------------------------------------------------------------
# 解析：T86 三大法人
# ----------------------------------------------------------------------


def _parse_t86(data: Dict[str, Any]) -> Dict[str, ChipDailyRow]:
    out: Dict[str, ChipDailyRow] = {}
    fields = data.get("fields") or []
    rows = data.get("data") or []
    if not fields or not rows:
        return out

    def col_idx(*keywords: str) -> int:
        for i, f in enumerate(fields):
            for kw in keywords:
                if kw in f:
                    return i
        return -1

    idx_code = col_idx("證券代號")
    idx_name = col_idx("證券名稱")
    idx_foreign = col_idx("外資及陸資買賣超股數", "外陸資買賣超股數", "外資買賣超股數")
    idx_trust = col_idx("投信買賣超股數")
    idx_dealer = col_idx("自營商買賣超股數")

    for row in rows:
        if idx_code < 0 or idx_code >= len(row):
            continue
        ticker = str(row[idx_code]).strip()
        if not ticker:
            continue
        try:
            foreign = _to_float(row[idx_foreign]) if idx_foreign >= 0 else 0.0
            trust = _to_float(row[idx_trust]) if idx_trust >= 0 else 0.0
            dealer = _to_float(row[idx_dealer]) if idx_dealer >= 0 else 0.0
        except Exception:
            continue
        out[ticker] = ChipDailyRow(
            ticker=ticker,
            name=str(row[idx_name]).strip() if 0 <= idx_name < len(row) else "",
            foreign_net=foreign / 1000.0,  # 換成「張」
            investment_trust_net=trust / 1000.0,
            dealer_net=dealer / 1000.0,
        )
    return out


def _parse_margin(data: Dict[str, Any]) -> Dict[str, tuple[float, float]]:
    """回傳 {ticker: (margin_balance, short_balance)} (張)。"""
    out: Dict[str, tuple[float, float]] = {}
    fields = data.get("fields") or []
    rows = data.get("data") or []
    if not fields or not rows:
        # MI_MARGN 回傳結構為 {tables: [{fields, data}]}
        tables = data.get("tables") or []
        for t in tables:
            f2 = t.get("fields") or []
            r2 = t.get("data") or []
            if "融資餘額" in "".join(f2):
                fields = f2
                rows = r2
                break
    if not fields or not rows:
        return out

    def idx(*kw: str) -> int:
        for i, f in enumerate(fields):
            for k in kw:
                if k in f:
                    return i
        return -1

    i_code = idx("股票代號", "證券代號")
    i_margin = idx("融資今日餘額", "融資餘額")
    i_short = idx("融券今日餘額", "融券餘額")
    if i_code < 0:
        return out
    for row in rows:
        if i_code >= len(row):
            continue
        ticker = str(row[i_code]).strip()
        if not ticker:
            continue
        m = _to_float(row[i_margin]) if i_margin >= 0 else 0.0
        s = _to_float(row[i_short]) if i_short >= 0 else 0.0
        out[ticker] = (m, s)
    return out


def _parse_borrow(data: Dict[str, Any]) -> Dict[str, float]:
    """回傳 {ticker: 借券賣出餘額(張)}。"""
    out: Dict[str, float] = {}
    fields = data.get("fields") or []
    rows = data.get("data") or []
    if not fields or not rows:
        return out

    def idx(*kw: str) -> int:
        for i, f in enumerate(fields):
            for k in kw:
                if k in f:
                    return i
        return -1

    i_code = idx("股票代號", "證券代號", "代號")
    i_balance = idx("當日借券賣出餘額", "借券賣出餘額", "餘額")
    if i_code < 0 or i_balance < 0:
        return out
    for row in rows:
        if i_code >= len(row):
            continue
        ticker = str(row[i_code]).strip()
        if not ticker:
            continue
        out[ticker] = _to_float(row[i_balance]) / 1000.0
    return out


def _parse_block(data: Dict[str, Any]) -> Dict[str, float]:
    """回傳 {ticker: 鉅額交易淨額(張)}。TWSE 沒有明確的買賣方向，先用成交量做累計。"""
    out: Dict[str, float] = {}
    rows = data.get("data") or []
    fields = data.get("fields") or []
    if not rows or not fields:
        return out

    def idx(*kw: str) -> int:
        for i, f in enumerate(fields):
            for k in kw:
                if k in f:
                    return i
        return -1

    i_code = idx("證券代號", "代號")
    i_vol = idx("成交股數", "股數")
    if i_code < 0 or i_vol < 0:
        return out
    for row in rows:
        if i_code >= len(row):
            continue
        ticker = str(row[i_code]).strip()
        if not ticker:
            continue
        vol = _to_float(row[i_vol]) / 1000.0
        out[ticker] = out.get(ticker, 0.0) + vol
    return out


# ----------------------------------------------------------------------
# 公開 API
# ----------------------------------------------------------------------


def fetch_daily_chips(
    date: dt.date,
    *,
    session: Optional[requests.Session] = None,
    root: Optional[Path] = None,
    use_cache: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, ChipDailyRow]:
    """抓某一日全市場個股的籌碼匯總。"""
    log = logger or get_logger("chips")
    sess = session or _new_session()

    t86 = _fetch_endpoint("qfii_daily", date, session=sess, root=root, use_cache=use_cache, logger=log)
    margin = _fetch_endpoint("margin_balance", date, session=sess, root=root, use_cache=use_cache, logger=log)
    borrow = _fetch_endpoint("borrow_balance", date, session=sess, root=root, use_cache=use_cache, logger=log)

    rows = _parse_t86(t86) if t86 else {}
    margin_map = _parse_margin(margin) if margin else {}
    borrow_map = _parse_borrow(borrow) if borrow else {}

    for ticker, (m, s) in margin_map.items():
        row = rows.setdefault(ticker, ChipDailyRow(ticker=ticker))
        row.margin_balance = m
        row.short_balance = s
    for ticker, b in borrow_map.items():
        row = rows.setdefault(ticker, ChipDailyRow(ticker=ticker))
        row.borrow_balance = b

    return rows


def _recent_trading_days(end_date: dt.date, days: int) -> List[dt.date]:
    """回傳含 end_date 在內的最近 N 個「非週末」日期 (不檢驗國定假日，TWSE 缺檔時 fetch 會自動跳過)。"""
    out: List[dt.date] = []
    cur = end_date
    while len(out) < days:
        if cur.weekday() < 5:  # 一~五
            out.append(cur)
        cur -= dt.timedelta(days=1)
    return out


def build_chip_summary(
    ticker: str,
    *,
    end_date: Optional[dt.date] = None,
    days: int = 5,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> ChipSummary:
    """個股近 N 日籌碼摘要。"""
    end = end_date or now_tw().date()
    dates = _recent_trading_days(end, days)
    sess = session or _new_session()

    rows: List[ChipDailyRow] = []
    foreign_total = 0.0
    trust_total = 0.0
    dealer_total = 0.0
    block_total = 0.0

    first_margin: Optional[float] = None
    last_margin: Optional[float] = None
    first_short: Optional[float] = None
    last_short: Optional[float] = None

    for d in reversed(dates):
        daily = fetch_daily_chips(
            d, session=sess, root=root, logger=logger,
        )
        row = daily.get(ticker)
        if row:
            rows.append(row)
            foreign_total += row.foreign_net
            trust_total += row.investment_trust_net
            dealer_total += row.dealer_net
            if first_margin is None:
                first_margin = row.margin_balance
                first_short = row.borrow_balance
            last_margin = row.margin_balance
            last_short = row.borrow_balance
        block = _fetch_endpoint("block_trade", d, session=sess, root=root, logger=logger)
        if block:
            bmap = _parse_block(block)
            block_total += bmap.get(ticker, 0.0)

    margin_pct = 0.0
    short_pct = 0.0
    if first_margin and first_margin > 0 and last_margin is not None:
        margin_pct = 100.0 * (last_margin - first_margin) / first_margin
    if first_short and first_short > 0 and last_short is not None:
        short_pct = 100.0 * (last_short - first_short) / first_short

    return ChipSummary(
        ticker=ticker,
        days=days,
        foreign_net=foreign_total,
        investment_trust_net=trust_total,
        dealer_net=dealer_total,
        margin_buy_change_pct=margin_pct,
        short_borrow_change_pct=short_pct,
        block_trade_net=block_total,
        rows=rows,
    )


def summary_to_chips_context(s: ChipSummary):
    """轉成 llm_analyzer.ChipsContext。"""
    from bot.llm_analyzer import ChipsContext
    return ChipsContext(
        foreign_net=s.foreign_net,
        investment_trust_net=s.investment_trust_net,
        dealer_net=s.dealer_net,
        margin_buy_change_pct=s.margin_buy_change_pct,
        short_borrow_change_pct=s.short_borrow_change_pct,
        block_trade_net=s.block_trade_net,
        notes=f"基於最近 {s.days} 交易日資料 (共 {len(s.rows)} 筆)",
    )


def summary_to_dict(s: ChipSummary) -> Dict[str, Any]:
    return {
        "ticker": s.ticker,
        "days": s.days,
        "foreign_net": s.foreign_net,
        "investment_trust_net": s.investment_trust_net,
        "dealer_net": s.dealer_net,
        "margin_buy_change_pct": s.margin_buy_change_pct,
        "short_borrow_change_pct": s.short_borrow_change_pct,
        "block_trade_net": s.block_trade_net,
        "rows": [asdict(r) for r in s.rows],
    }


# ----------------------------------------------------------------------
# 輔助
# ----------------------------------------------------------------------


def _to_float(x: Any) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").replace("--", "0").strip()
    if not s or s in ("-", "--"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


__all__ = [
    "ChipDailyRow",
    "ChipSummary",
    "build_chip_summary",
    "fetch_daily_chips",
    "summary_to_chips_context",
    "summary_to_dict",
]

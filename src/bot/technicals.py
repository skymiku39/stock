"""technicals -- 抓 TWSE 日 K 線並計算技術指標 (MA/MACD/RSI/KD)。

設計目標
========
* 純 pandas 實作，不引入 ta-lib 等需要 C 編譯的套件
* TWSE 端點：`/rwd/zh/afterTrading/STOCK_DAY` 每次回一個月，可滾動回補多月
* 結果以 `data/technicals/<ticker>/daily_kline.csv` 快取，可跨月增量
* 提供 `build_technical_snapshot()` 一次取出歷史K + 全部指標 + 衍生訊號
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from bot.cloud_file_cache import mirror_file_to_cloud, restore_file_from_cloud
from bot.utils import get_logger, mk_folder, now_tw

# ----------------------------------------------------------------------
# 抓取
# ----------------------------------------------------------------------

URL_STOCK_DAY = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
    "?response=json&date={ym}01&stockNo={ticker}"
)

# 上櫃 (TPEx) 個股單月日K
URL_TPEX_STOCK_DAY = (
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
    "?code={ticker}&date={year:04d}/{month:02d}/01&response=json"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Referer": "https://www.twse.com.tw/zh/",
    })
    return s


def _cache_dir(ticker: str, root: Path | None = None) -> Path:
    base = (root or Path.cwd()) / "data" / "technicals" / ticker
    mk_folder(str(base))
    return base


def _resolve_market(
    ticker: str,
    root: Path | None,
    *,
    logger: logging.Logger | None = None,
) -> str:
    """判斷個股市場別；未知一律當上市 (維持既有行為)。"""
    try:
        from bot.market_meta import TPEX, detect_market
        return TPEX if detect_market(ticker, root=root, logger=logger) == TPEX else "twse"
    except Exception:
        return "twse"


def _roc_to_iso(roc: str) -> dt.date | None:
    """民國日期 (e.g. 114/11/03) 轉為 ISO date。"""
    try:
        parts = roc.split("/")
        if len(parts) != 3:
            return None
        y = int(parts[0])
        if y < 1911:
            y += 1911
        return dt.date(y, int(parts[1]), int(parts[2]))
    except Exception:
        return None


def _to_float(x: Any) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").replace("--", "").strip()
    if not s or s in ("-", "X"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


@dataclass
class MonthlyKlineResult:
    """單月日 K 抓取結果（含 HTTP 狀態，供慢速排程判斷限流）。"""

    rows: list[dict[str, Any]]
    http_status: int = 0
    error: str = ""


def fetch_monthly_kline_with_meta(
    ticker: str,
    year: int,
    month: int,
    *,
    market: str = "twse",
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> MonthlyKlineResult:
    """抓單月日 K 並回傳 HTTP 狀態。"""
    if market == "tpex":
        rows = _fetch_tpex_monthly_kline(
            ticker, year, month, session=session, logger=logger,
        )
        return MonthlyKlineResult(rows=rows, http_status=200 if rows else 0)

    log = logger or get_logger("technicals")
    sess = session or _session()
    ym = f"{year:04d}{month:02d}"
    url = URL_STOCK_DAY.format(ym=ym, ticker=ticker)
    try:
        resp = sess.get(url, timeout=20)
        status = int(resp.status_code)
        if status != 200:
            log.warning("%s/%s K 線 HTTP %d", ticker, ym, status)
            return MonthlyKlineResult(rows=[], http_status=status)
        data = resp.json()
        time.sleep(0.4)
    except Exception as exc:
        log.exception("%s/%s K 線抓取失敗", ticker, ym)
        return MonthlyKlineResult(rows=[], http_status=0, error=str(exc))

    if not isinstance(data, dict) or data.get("stat") != "OK":
        return MonthlyKlineResult(rows=[], http_status=status)

    fields = data.get("fields") or []
    raw_rows = data.get("data") or []
    if not fields or not raw_rows:
        return MonthlyKlineResult(rows=[], http_status=status)

    def idx(*kw: str) -> int:
        for i, f in enumerate(fields):
            for k in kw:
                if k in f:
                    return i
        return -1

    i_date = idx("日期")
    i_vol = idx("成交股數", "成交量")
    i_open = idx("開盤價", "開盤")
    i_high = idx("最高價", "最高")
    i_low = idx("最低價", "最低")
    i_close = idx("收盤價", "收盤")
    out: list[dict[str, Any]] = []
    for r in raw_rows:
        if i_date < 0 or i_date >= len(r):
            continue
        d = _roc_to_iso(str(r[i_date]).strip())
        if not d:
            continue
        out.append({
            "date": d.isoformat(),
            "open": _to_float(r[i_open]) if i_open >= 0 else 0.0,
            "high": _to_float(r[i_high]) if i_high >= 0 else 0.0,
            "low": _to_float(r[i_low]) if i_low >= 0 else 0.0,
            "close": _to_float(r[i_close]) if i_close >= 0 else 0.0,
            "volume": _to_float(r[i_vol]) / 1000.0 if i_vol >= 0 else 0.0,
        })
    return MonthlyKlineResult(rows=out, http_status=status)


def fetch_monthly_kline(
    ticker: str,
    year: int,
    month: int,
    *,
    market: str = "twse",
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """抓單月個股日K。

    Args:
        market: 'twse' (上市) 或 'tpex' (上櫃)；其他值一律視為上市。

    回傳 list[{date, open, high, low, close, volume(張)}]，依日期升冪。
    """
    if market == "tpex":
        return _fetch_tpex_monthly_kline(ticker, year, month, session=session, logger=logger)
    return fetch_monthly_kline_with_meta(
        ticker, year, month, market=market, session=session, logger=logger,
    ).rows


def fetch_monthly_kline_yfinance(
    ticker: str,
    year: int,
    month: int,
    *,
    market: str = "twse",
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """以 yfinance 補單月日 K（TWSE 403 時的備援來源）。"""
    log = logger or get_logger("technicals")
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        log.warning("yfinance 未安裝，無法備援日 K")
        return []

    start_d = dt.date(year, month, 1)
    if month == 12:
        end_d = dt.date(year, 12, 31)
    else:
        end_d = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    end_excl = (end_d + dt.timedelta(days=1)).isoformat()

    suffixes = (".TWO", ".TW") if market == "tpex" else (".TW", ".TWO")
    for suffix in suffixes:
        sym = f"{ticker}{suffix}"
        try:
            hist = yf.Ticker(sym).history(
                start=start_d.isoformat(),
                end=end_excl,
                auto_adjust=False,
            )
        except Exception as exc:
            log.warning("%s yfinance %04d/%02d 例外: %s", ticker, year, month, exc)
            continue
        if hist is None or hist.empty:
            continue
        out: list[dict[str, Any]] = []
        for idx, row in hist.iterrows():
            try:
                d = idx.date() if hasattr(idx, "date") else dt.date.fromisoformat(str(idx)[:10])
            except Exception:
                continue
            if d < start_d or d > end_d:
                continue
            out.append({
                "date": d.isoformat(),
                "open": float(row.get("Open", 0) or 0),
                "high": float(row.get("High", 0) or 0),
                "low": float(row.get("Low", 0) or 0),
                "close": float(row.get("Close", 0) or 0),
                "volume": float(row.get("Volume", 0) or 0) / 1000.0,
            })
        if out:
            return sorted(out, key=lambda r: r["date"])
    return []


def _fetch_tpex_monthly_kline(
    ticker: str,
    year: int,
    month: int,
    *,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """抓 TPEx 上櫃單月個股日K。

    TPEx 回傳格式：{tables:[{fields:[日期,成交張數,成交仟元,開盤,最高,最低,收盤,漲跌,筆數], data:[...]}]}
    日期為民國 (115/05/04)；成交張數已是「張」。
    """
    log = logger or get_logger("technicals")
    sess = session or _session()
    url = URL_TPEX_STOCK_DAY.format(ticker=ticker, year=year, month=month)
    try:
        resp = sess.get(url, timeout=20)
        if resp.status_code != 200:
            log.warning("%s %04d/%02d 上櫃K線 HTTP %d", ticker, year, month, resp.status_code)
            return []
        data = resp.json()
        time.sleep(0.4)
    except Exception:
        log.exception("%s %04d/%02d 上櫃K線抓取失敗", ticker, year, month)
        return []

    tables = data.get("tables") if isinstance(data, dict) else None
    if not tables:
        return []
    table = tables[0] or {}
    fields = table.get("fields") or []
    rows = table.get("data") or []
    if not fields or not rows:
        return []

    def idx(*kw: str) -> int:
        for i, f in enumerate(fields):
            fs = str(f).replace(" ", "")
            for k in kw:
                if k in fs:
                    return i
        return -1

    i_date = idx("日期")
    i_vol = idx("成交張數", "成交股數")
    i_open = idx("開盤")
    i_high = idx("最高")
    i_low = idx("最低")
    i_close = idx("收盤")
    out: list[dict[str, Any]] = []
    for r in rows:
        if i_date < 0 or i_date >= len(r):
            continue
        d = _roc_to_iso(str(r[i_date]).strip())
        if not d:
            continue
        out.append({
            "date": d.isoformat(),
            "open": _to_float(r[i_open]) if i_open >= 0 else 0.0,
            "high": _to_float(r[i_high]) if i_high >= 0 else 0.0,
            "low": _to_float(r[i_low]) if i_low >= 0 else 0.0,
            "close": _to_float(r[i_close]) if i_close >= 0 else 0.0,
            "volume": _to_float(r[i_vol]) if i_vol >= 0 else 0.0,  # TPEx 已是「張」
        })
    out.sort(key=lambda x: x["date"])
    return out


def fetch_recent_kline(
    ticker: str,
    *,
    months: int = 6,
    end_date: dt.date | None = None,
    root: Path | None = None,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
    incremental: bool = True,
    save_to_db: bool = True,
) -> pd.DataFrame:
    """抓近 N 個月 K 線；本地有快取則只補新月份。

    抓完同時寫入：
    1. CSV 快取  -- `data/technicals/<ticker>/daily_kline.csv`
    2. SQLite price_history table  (`save_to_db=True`，預設開啟)

    Returns:
        DataFrame: columns = ['date', 'open', 'high', 'low', 'close', 'volume']
        依日期升冪排序。
    """
    log = logger or get_logger("technicals")
    end = end_date or now_tw().date()
    sess = session or _session()
    market = _resolve_market(ticker, root, logger=log)
    cache_path = _cache_dir(ticker, root) / "daily_kline.csv"
    restore_file_from_cloud(cache_path, root=root)

    existing: pd.DataFrame | None = None
    if incremental and cache_path.exists():
        try:
            existing = pd.read_csv(cache_path, dtype={"date": str})
        except Exception:
            existing = None

    months_back: list[tuple[int, int]] = []
    cur = end.replace(day=1)
    for _ in range(months):
        months_back.append((cur.year, cur.month))
        prev = cur - dt.timedelta(days=1)
        cur = prev.replace(day=1)
    months_back.reverse()

    if existing is not None and not existing.empty:
        latest = existing["date"].max()
        try:
            latest_d = dt.date.fromisoformat(latest)
            # 只重抓 latest 那個月 + 之後的月份 (確保資料一致性)
            months_back = [
                (y, m) for (y, m) in months_back
                if dt.date(y, m, 1) >= latest_d.replace(day=1)
            ]
        except Exception:
            pass

    rows: list[dict[str, Any]] = []
    for (y, m) in months_back:
        rows.extend(fetch_monthly_kline(
            ticker, y, m, market=market, session=sess, logger=log,
        ))

    if not rows and existing is not None and not existing.empty:
        df_out = _coerce_df(existing).sort_values("date").reset_index(drop=True)
        if save_to_db:
            _save_df_to_db(ticker, df_out, root=root, logger=log)
        return df_out

    new_df = pd.DataFrame(rows)
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, new_df], ignore_index=True)
    else:
        merged = new_df
    merged = _coerce_df(merged)
    merged = merged.drop_duplicates(subset=["date"], keep="last")
    merged = merged.sort_values("date").reset_index(drop=True)
    merged.to_csv(cache_path, index=False, encoding="utf-8")
    mirror_file_to_cloud(cache_path, root=root)

    if save_to_db and not merged.empty:
        # 只寫入新抓到的那些日期 (若 incremental) 或全部 (若無 cache)
        try:
            if rows:
                fresh_dates = {r["date"] for r in rows}
                fresh = merged[merged["date"].isin(fresh_dates)]
            else:
                fresh = merged
            _save_df_to_db(ticker, fresh, root=root, logger=log)
        except Exception:
            log.exception("%s 寫入 price_history DB 失敗 (忽略)", ticker)
    return merged


def _save_df_to_db(
    ticker: str,
    df: pd.DataFrame,
    *,
    root: Path | None = None,
    logger: logging.Logger | None = None,
) -> int:
    """把 K 線 DataFrame 批次寫入 SQLite price_history。失敗時記錄並回 0。"""
    log = logger or get_logger("technicals")
    if df is None or df.empty:
        return 0
    try:
        from bot.stock_db import PriceBar, StockDB, default_db_path
    except Exception:
        log.warning("找不到 stock_db 模組；略過 DB 寫入")
        return 0
    try:
        db = StockDB.open(path=default_db_path(root))
        bars = []
        for _, r in df.iterrows():
            d = str(r.get("date", "")).strip()
            if not d:
                continue
            bars.append(PriceBar(
                symbol=ticker,
                date=d,
                open=float(r.get("open", 0) or 0),
                high=float(r.get("high", 0) or 0),
                low=float(r.get("low", 0) or 0),
                close=float(r.get("close", 0) or 0),
                volume=float(r.get("volume", 0) or 0),
                source="twse",
            ))
        if not bars:
            return 0
        return db.bulk_upsert_price_bars(bars)
    except Exception:
        log.exception("%s 寫入 DB 例外", ticker)
        return 0


# ----------------------------------------------------------------------
# 長區間 / 分批向過去抓取
# ----------------------------------------------------------------------


def _enumerate_months(start: dt.date, end: dt.date) -> list[tuple[int, int]]:
    """[start, end] 之間（含）所有月份 (year, month)，由舊到新。"""
    out: list[tuple[int, int]] = []
    cur = start.replace(day=1)
    last = end.replace(day=1)
    while cur <= last:
        out.append((cur.year, cur.month))
        # 推到下個月 1 號
        if cur.month == 12:
            cur = dt.date(cur.year + 1, 1, 1)
        else:
            cur = dt.date(cur.year, cur.month + 1, 1)
    return out


def get_kline_coverage(
    ticker: str,
    *,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """回傳目前 K 線快取範圍 (earliest, latest, rows)。優先讀 DB，否則讀 CSV。

    若無資料回 None。
    """
    df = load_kline_from_db(ticker, root=root)
    if df is None or df.empty:
        cache_path = _cache_dir(ticker, root) / "daily_kline.csv"
        restore_file_from_cloud(cache_path, root=root)
        if cache_path.exists():
            try:
                df = pd.read_csv(cache_path, dtype={"date": str})
                df = _coerce_df(df)
            except Exception:
                df = pd.DataFrame()
    if df is None or df.empty:
        return None
    earliest = str(df["date"].min())
    latest = str(df["date"].max())
    return {
        "earliest": earliest,
        "latest": latest,
        "rows": len(df),
    }


def fetch_kline_range(
    ticker: str,
    *,
    start_date: dt.date,
    end_date: dt.date | None = None,
    root: Path | None = None,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
    save_to_db: bool = True,
    skip_existing_months: bool = True,
    on_progress: Callable[[int, int, str, int], None] | None = None,
    request_delay_sec: float = 0.4,
    direction: str = "backward",  # "backward" (新→舊) 或 "forward" (舊→新)
) -> pd.DataFrame:
    """抓 [start_date, end_date] 之間的所有日 K，分月 fetch + 進度回呼。

    Args:
        skip_existing_months: True 時若該月在 cache 中已有任何資料就跳過 (但仍會抓 cache 邊界月)
        on_progress: 接收 (current, total, label, fetched_rows) 的 callback
        direction: backward = 從最近月份往過去抓 (符合 TWSE 限制較快)；forward = 從最早往現在抓
        request_delay_sec: 每次 HTTP 之間 sleep；避免被 TWSE rate limit

    回傳合併後的完整 DataFrame。
    """
    log = logger or get_logger("technicals")
    sess = session or _session()
    market = _resolve_market(ticker, root, logger=log)
    end = end_date or now_tw().date()
    if start_date > end:
        return pd.DataFrame()

    cache_path = _cache_dir(ticker, root) / "daily_kline.csv"
    restore_file_from_cloud(cache_path, root=root)
    existing: pd.DataFrame | None = None
    if cache_path.exists():
        try:
            existing = pd.read_csv(cache_path, dtype={"date": str})
            existing = _coerce_df(existing)
        except Exception:
            existing = None

    existing_months: set[tuple[int, int]] = set()
    if existing is not None and not existing.empty and skip_existing_months:
        for d in existing["date"].tolist():
            try:
                dd = dt.date.fromisoformat(str(d)[:10])
                existing_months.add((dd.year, dd.month))
            except Exception:
                continue

    months = _enumerate_months(start_date, end)
    if direction == "backward":
        months.reverse()

    total = len(months)
    rows: list[dict[str, Any]] = []
    fetched_months = 0
    for idx, (y, m) in enumerate(months, start=1):
        # 邊界月：cache 中可能只有部分日期 → 不跳過邊界月
        is_boundary = False
        if existing is not None and not existing.empty:
            min_d = dt.date.fromisoformat(str(existing["date"].min())[:10])
            max_d = dt.date.fromisoformat(str(existing["date"].max())[:10])
            is_boundary = (
                (y, m) == (min_d.year, min_d.month)
                or (y, m) == (max_d.year, max_d.month)
            )
        if (y, m) in existing_months and not is_boundary:
            if on_progress:
                on_progress(idx, total, f"{y}/{m:02d} 已有快取", 0)
            continue

        label = f"{y}/{m:02d}"
        try:
            month_rows = fetch_monthly_kline(
                ticker, y, m, market=market, session=sess, logger=log,
            )
        except Exception as e:
            log.warning("%s %s 抓取例外：%s", ticker, label, e)
            month_rows = []

        rows.extend(month_rows)
        fetched_months += 1 if month_rows else 0
        if on_progress:
            on_progress(idx, total, label, len(month_rows))

        if request_delay_sec > 0:
            time.sleep(request_delay_sec)

    if not rows:
        # 沒抓到新的，回傳原有
        if existing is not None and not existing.empty:
            return existing.sort_values("date").reset_index(drop=True)
        return pd.DataFrame()

    new_df = pd.DataFrame(rows)
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, new_df], ignore_index=True)
    else:
        merged = new_df
    merged = _coerce_df(merged)
    merged = merged.drop_duplicates(subset=["date"], keep="last")
    merged = merged.sort_values("date").reset_index(drop=True)
    merged.to_csv(cache_path, index=False, encoding="utf-8")
    mirror_file_to_cloud(cache_path, root=root)

    if save_to_db and not merged.empty:
        try:
            fresh_dates = {r["date"] for r in rows}
            fresh = merged[merged["date"].isin(fresh_dates)]
            _save_df_to_db(ticker, fresh, root=root, logger=log)
        except Exception:
            log.exception("%s 寫入 price_history DB 失敗 (忽略)", ticker)

    return merged


def extend_kline_backward(
    ticker: str,
    *,
    years_back: int = 5,
    root: Path | None = None,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
    save_to_db: bool = True,
    on_progress: Callable[[int, int, str, int], None] | None = None,
    request_delay_sec: float = 0.4,
) -> pd.DataFrame:
    """從目前 cache 中「最早日期」再往前回補 N 年（若無 cache 則以今天為起點）。

    這是 K 線看板「往前抓 5 年/10 年」按鈕的後端。
    """
    cov = get_kline_coverage(ticker, root=root)
    if cov:
        try:
            anchor = dt.date.fromisoformat(cov["earliest"])
        except Exception:
            anchor = now_tw().date()
    else:
        anchor = now_tw().date()
    target_start = anchor.replace(day=1)
    # 往前 N 年
    try:
        target_start = target_start.replace(year=target_start.year - years_back)
    except ValueError:
        target_start = target_start.replace(
            year=target_start.year - years_back, day=28,
        )
    # 結束在 anchor 所在月的「前一個月」末日 (避免重抓 anchor 月)
    if cov:
        end_date = anchor.replace(day=1) - dt.timedelta(days=1)
    else:
        end_date = now_tw().date()
    if end_date < target_start:
        return pd.DataFrame()
    return fetch_kline_range(
        ticker,
        start_date=target_start,
        end_date=end_date,
        root=root,
        session=session,
        logger=logger,
        save_to_db=save_to_db,
        on_progress=on_progress,
        request_delay_sec=request_delay_sec,
        direction="backward",
    )


def load_kline_from_db(
    ticker: str,
    *,
    months: int | None = None,
    start: str | None = None,
    end: str | None = None,
    root: Path | None = None,
) -> pd.DataFrame:
    """從 SQLite 讀回 K 線 DataFrame；找不到資料回空 DataFrame。

    優先使用 `start` / `end` 區間；否則使用 `months` 取最新 ~N 個月 (約 22*N 筆)。
    回傳格式與 fetch_recent_kline 一致：columns 含 date/open/high/low/close/volume。
    """
    try:
        from bot.stock_db import StockDB, default_db_path
    except Exception:
        return pd.DataFrame()
    try:
        db = StockDB.open(path=default_db_path(root))
        if start or end:
            bars = db.get_price_history(ticker, start=start, end=end)
        elif months:
            bars = db.get_price_history(ticker, limit=int(months) * 25)
        else:
            bars = db.get_price_history(ticker)
        if not bars:
            return pd.DataFrame()
        df = pd.DataFrame([{
            "date": b.date,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        } for b in bars])
        return _coerce_df(df).sort_values("date").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def _coerce_df(df: pd.DataFrame) -> pd.DataFrame:
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    return df


# ----------------------------------------------------------------------
# 技術指標
# ----------------------------------------------------------------------


def add_moving_averages(
    df: pd.DataFrame,
    periods: list[int] = (5, 10, 20, 60, 120),
) -> pd.DataFrame:
    out = df.copy()
    for p in periods:
        out[f"ma{p}"] = out["close"].rolling(p, min_periods=1).mean()
        out[f"vol_ma{p}"] = out["volume"].rolling(p, min_periods=1).mean()
    return out


def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """RSI = 100 - 100 / (1 + RS)；RS = avg_gain / avg_loss (Wilder 平滑)。"""
    out = df.copy()
    delta = out["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    out[f"rsi{period}"] = 100 - (100 / (1 + rs))
    out[f"rsi{period}"] = out[f"rsi{period}"].fillna(50.0)
    return out


def add_macd(
    df: pd.DataFrame,
    fast: int = 12, slow: int = 26, signal: int = 9,
) -> pd.DataFrame:
    out = df.copy()
    ema_fast = out["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = out["close"].ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    out["macd"] = macd
    out["macd_signal"] = sig
    out["macd_hist"] = macd - sig
    return out


def add_kd(df: pd.DataFrame, period: int = 9) -> pd.DataFrame:
    """KD (Stochastic) 9-3-3。"""
    out = df.copy()
    low_min = out["low"].rolling(period, min_periods=1).min()
    high_max = out["high"].rolling(period, min_periods=1).max()
    rsv = 100 * (out["close"] - low_min) / (high_max - low_min).replace(0, pd.NA)
    rsv = rsv.fillna(50.0)
    k_list: list[float] = []
    d_list: list[float] = []
    k_prev = 50.0
    d_prev = 50.0
    for r in rsv.tolist():
        k = (2 / 3) * k_prev + (1 / 3) * r
        d = (2 / 3) * d_prev + (1 / 3) * k
        k_list.append(k)
        d_list.append(d)
        k_prev, d_prev = k, d
    out["k"] = k_list
    out["d"] = d_list
    return out


def add_bollinger(
    df: pd.DataFrame, period: int = 20, std_mult: float = 2.0,
) -> pd.DataFrame:
    out = df.copy()
    mid = out["close"].rolling(period, min_periods=1).mean()
    sd = out["close"].rolling(period, min_periods=1).std()
    out["boll_mid"] = mid
    out["boll_upper"] = mid + std_mult * sd
    out["boll_lower"] = mid - std_mult * sd
    return out


def compute_indicators(
    df: pd.DataFrame,
    *,
    ma_periods: list[int] = (5, 10, 20, 60, 120),
    rsi_period: int = 14,
    macd: tuple = (12, 26, 9),
    kd_period: int = 9,
    boll_period: int = 20,
) -> pd.DataFrame:
    """一次計算 MA / RSI / MACD / KD / 布林。"""
    if df.empty:
        return df
    out = df.copy()
    out = add_moving_averages(out, ma_periods)
    out = add_rsi(out, rsi_period)
    out = add_macd(out, *macd)
    out = add_kd(out, kd_period)
    out = add_bollinger(out, boll_period)
    return out


# ----------------------------------------------------------------------
# 衍生訊號
# ----------------------------------------------------------------------


@dataclass
class TechnicalSignal:
    """單一技術訊號 (bullish / bearish / neutral)。"""

    label: str
    bullish: bool | None
    detail: str


def derive_signals(df_with_indicators: pd.DataFrame) -> list[TechnicalSignal]:
    """依最新一根 K 線判讀常見訊號。"""
    if df_with_indicators is None or df_with_indicators.empty:
        return []
    last = df_with_indicators.iloc[-1]
    prev = df_with_indicators.iloc[-2] if len(df_with_indicators) >= 2 else last
    signals: list[TechnicalSignal] = []

    close = float(last["close"])
    ma20 = float(last.get("ma20", close))
    ma60 = float(last.get("ma60", close))
    ma5 = float(last.get("ma5", close))

    signals.append(TechnicalSignal(
        label="月線位置",
        bullish=close >= ma20,
        detail=f"收盤 {close:.2f} {'≥' if close >= ma20 else '<'} 月線 {ma20:.2f}",
    ))
    signals.append(TechnicalSignal(
        label="季線多空",
        bullish=close >= ma60,
        detail=f"收盤 {close:.2f} {'≥' if close >= ma60 else '<'} 季線 {ma60:.2f}",
    ))

    if "macd" in last and "macd_signal" in last:
        macd = float(last["macd"])
        sig = float(last["macd_signal"])
        prev_macd = float(prev["macd"])
        prev_sig = float(prev["macd_signal"])
        cross_up = prev_macd < prev_sig and macd > sig
        cross_dn = prev_macd > prev_sig and macd < sig
        bullish = None
        detail = f"MACD {macd:.2f} / Signal {sig:.2f}"
        if cross_up:
            bullish = True
            detail += "；剛黃金交叉"
        elif cross_dn:
            bullish = False
            detail += "；剛死亡交叉"
        else:
            bullish = macd > sig
            detail += "；柱狀圖 " + ("↑" if macd > sig else "↓")
        signals.append(TechnicalSignal(label="MACD", bullish=bullish, detail=detail))

    if "rsi14" in last:
        r = float(last["rsi14"])
        if r >= 70:
            signals.append(TechnicalSignal("RSI 超買", False, f"RSI14 = {r:.1f} ≥ 70"))
        elif r <= 30:
            signals.append(TechnicalSignal("RSI 超賣", True, f"RSI14 = {r:.1f} ≤ 30"))
        else:
            signals.append(TechnicalSignal("RSI 區間", None, f"RSI14 = {r:.1f}"))

    if "k" in last and "d" in last:
        k = float(last["k"])
        d = float(last["d"])
        bullish = k > d
        signals.append(TechnicalSignal(
            label="KD",
            bullish=bullish,
            detail=f"K={k:.1f}, D={d:.1f}",
        ))

    if "volume" in last and "vol_ma20" in last:
        vol = float(last["volume"])
        vma = float(last["vol_ma20"]) or 1.0
        ratio = vol / vma if vma else 1.0
        signals.append(TechnicalSignal(
            label="量比",
            bullish=ratio >= 1.2 and close >= prev["close"],
            detail=f"量 {vol:.0f} 張 / 月均 {vma:.0f} 張 ({ratio:.2f}x)",
        ))

    # 均線多頭排列 (5 > 20 > 60)
    if all(k in last for k in ("ma5", "ma20", "ma60")):
        bull = ma5 > ma20 > ma60
        bear = ma5 < ma20 < ma60
        signals.append(TechnicalSignal(
            label="均線排列",
            bullish=bull if bull or bear else None,
            detail=f"MA5={ma5:.2f}, MA20={ma20:.2f}, MA60={ma60:.2f}"
                   + ("（多頭排列）" if bull else "（空頭排列）" if bear else ""),
        ))

    # K 線型態 (單根 K 棒，依「量化通」16 種型態分類)
    try:
        from bot.candle_patterns import classify_latest
        cp = classify_latest(df_with_indicators)
        if cp is not None:
            signals.append(TechnicalSignal(
                label=f"K線型態：{cp.name}",
                bullish=cp.bias,
                detail=cp.meaning,
            ))
    except Exception:
        pass

    return signals


# ----------------------------------------------------------------------
# Snapshot
# ----------------------------------------------------------------------


@dataclass
class TechnicalSnapshot:
    ticker: str
    fetched_at: str = ""
    last_date: str = ""
    last_close: float = 0.0
    pct_change_1d: float = 0.0
    pct_change_5d: float = 0.0
    pct_change_20d: float = 0.0
    pct_change_60d: float = 0.0
    rsi14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    k: float | None = None
    d: float | None = None
    ma5: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    ma120: float | None = None
    volume_last: float | None = None
    vol_ma20: float | None = None
    boll_upper: float | None = None
    boll_lower: float | None = None
    signals: list[dict[str, Any]] = field(default_factory=list)
    rows: int = 0
    technical_score: float = 50.0   # 0-100 (給 scoring.py 用)
    # K 線型態 (最新一根 K 棒)
    candle_pattern_id: str = ""
    candle_pattern: str = ""        # 中文型態名
    candle_category: str = ""       # 實體 / 上影線 / 下影線 / 上下影線 / 十字線
    candle_bias: bool | None = None
    candle_meaning: str = ""

    @property
    def has_data(self) -> bool:
        return self.rows > 0


def build_technical_snapshot(
    ticker: str,
    *,
    months: int = 6,
    root: Path | None = None,
    session: requests.Session | None = None,
    logger: logging.Logger | None = None,
    refresh: bool = True,
) -> tuple[TechnicalSnapshot, pd.DataFrame]:
    """組合單一個股的技術面 snapshot + 完整 DataFrame（含指標）。

    Returns:
        (snapshot, df_with_indicators)
    """
    if refresh:
        df = fetch_recent_kline(
            ticker, months=months,
            root=root, session=session, logger=logger,
        )
    else:
        # 優先讀 SQLite price_history (跨機可同步)；無資料時退回 CSV 快取
        df = load_kline_from_db(ticker, months=months, root=root)
        if df is None or df.empty:
            cache_path = _cache_dir(ticker, root) / "daily_kline.csv"
            restore_file_from_cloud(cache_path, root=root)
            if not cache_path.exists():
                df = pd.DataFrame()
            else:
                try:
                    df = pd.read_csv(cache_path, dtype={"date": str})
                    df = _coerce_df(df)
                except Exception:
                    df = pd.DataFrame()

    if df.empty:
        return TechnicalSnapshot(
            ticker=ticker,
            fetched_at=now_tw().isoformat(timespec="seconds"),
        ), df

    df_ind = compute_indicators(df)
    last = df_ind.iloc[-1]
    snap = TechnicalSnapshot(
        ticker=ticker,
        fetched_at=now_tw().isoformat(timespec="seconds"),
        last_date=str(last["date"]),
        last_close=float(last["close"]),
        pct_change_1d=_pct_change(df_ind, 1),
        pct_change_5d=_pct_change(df_ind, 5),
        pct_change_20d=_pct_change(df_ind, 20),
        pct_change_60d=_pct_change(df_ind, 60),
        rsi14=_safe_float(last.get("rsi14")),
        macd=_safe_float(last.get("macd")),
        macd_signal=_safe_float(last.get("macd_signal")),
        macd_hist=_safe_float(last.get("macd_hist")),
        k=_safe_float(last.get("k")),
        d=_safe_float(last.get("d")),
        ma5=_safe_float(last.get("ma5")),
        ma20=_safe_float(last.get("ma20")),
        ma60=_safe_float(last.get("ma60")),
        ma120=_safe_float(last.get("ma120")),
        volume_last=_safe_float(last.get("volume")),
        vol_ma20=_safe_float(last.get("vol_ma20")),
        boll_upper=_safe_float(last.get("boll_upper")),
        boll_lower=_safe_float(last.get("boll_lower")),
        rows=len(df_ind),
    )
    sigs = derive_signals(df_ind)
    snap.signals = [asdict(s) for s in sigs]
    snap.technical_score = _score_from_signals(sigs, snap)

    # K 線型態 (最新一根)
    try:
        from bot.candle_patterns import classify_latest
        cp = classify_latest(df_ind)
        if cp is not None:
            snap.candle_pattern_id = cp.pattern_id
            snap.candle_pattern = cp.name
            snap.candle_category = cp.category
            snap.candle_bias = cp.bias
            snap.candle_meaning = cp.meaning
    except Exception:
        pass

    return snap, df_ind


def _pct_change(df: pd.DataFrame, n: int) -> float:
    if len(df) <= n:
        return 0.0
    last_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-1 - n])
    if prev_close <= 0:
        return 0.0
    return round(100.0 * (last_close - prev_close) / prev_close, 2)


def _safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except Exception:
        pass
    try:
        return round(float(x), 4)
    except Exception:
        return None


def _score_from_signals(
    signals: list[TechnicalSignal],
    snap: TechnicalSnapshot,
) -> float:
    """把訊號 + 漲跌幅換算為 0-100 的技術面分數。"""
    base = 50.0
    for s in signals:
        if s.bullish is True:
            base += 5.0
        elif s.bullish is False:
            base -= 5.0
    # 額外：站上長均線
    if snap.last_close and snap.ma60 and snap.last_close > snap.ma60:
        base += 4.0
    if snap.last_close and snap.ma120 and snap.last_close > snap.ma120:
        base += 3.0
    # 近 20 日表現
    base += max(-10.0, min(10.0, snap.pct_change_20d / 2))
    return max(0.0, min(100.0, base))


def snapshot_to_dict(s: TechnicalSnapshot) -> dict[str, Any]:
    return asdict(s)


__all__ = [
    "TechnicalSignal",
    "TechnicalSnapshot",
    "add_bollinger",
    "add_kd",
    "add_macd",
    "add_moving_averages",
    "add_rsi",
    "build_technical_snapshot",
    "compute_indicators",
    "derive_signals",
    "extend_kline_backward",
    "fetch_kline_range",
    "fetch_monthly_kline",
    "fetch_recent_kline",
    "get_kline_coverage",
    "load_kline_from_db",
    "snapshot_to_dict",
]

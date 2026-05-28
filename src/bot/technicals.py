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
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from bot.utils import get_logger, mk_folder, now_tw


# ----------------------------------------------------------------------
# 抓取
# ----------------------------------------------------------------------

URL_STOCK_DAY = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
    "?response=json&date={ym}01&stockNo={ticker}"
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
    })
    return s


def _cache_dir(ticker: str, root: Optional[Path] = None) -> Path:
    base = (root or Path.cwd()) / "data" / "technicals" / ticker
    mk_folder(str(base))
    return base


def _roc_to_iso(roc: str) -> Optional[dt.date]:
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


def fetch_monthly_kline(
    ticker: str,
    year: int,
    month: int,
    *,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """抓 TWSE 單月個股日K (民國月份；TWSE 端點接受西元，但路徑要 YYYYMMDD)。

    回傳 list[{date, open, high, low, close, volume, ...}]，依日期升冪。
    """
    log = logger or get_logger("technicals")
    sess = session or _session()
    ym = f"{year:04d}{month:02d}"
    url = URL_STOCK_DAY.format(ym=ym, ticker=ticker)
    try:
        resp = sess.get(url, timeout=20)
        if resp.status_code != 200:
            log.warning("%s/%s K 線 HTTP %d", ticker, ym, resp.status_code)
            return []
        data = resp.json()
        time.sleep(0.4)
    except Exception:
        log.exception("%s/%s K 線抓取失敗", ticker, ym)
        return []

    if not isinstance(data, dict) or data.get("stat") != "OK":
        return []
    fields = data.get("fields") or []
    rows = data.get("data") or []
    if not fields or not rows:
        return []

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
    out: List[Dict[str, Any]] = []
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
            "volume": _to_float(r[i_vol]) / 1000.0 if i_vol >= 0 else 0.0,  # 張
        })
    return out


def fetch_recent_kline(
    ticker: str,
    *,
    months: int = 6,
    end_date: Optional[dt.date] = None,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
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
    cache_path = _cache_dir(ticker, root) / "daily_kline.csv"

    existing: Optional[pd.DataFrame] = None
    if incremental and cache_path.exists():
        try:
            existing = pd.read_csv(cache_path, dtype={"date": str})
        except Exception:
            existing = None

    months_back: List[tuple[int, int]] = []
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

    rows: List[Dict[str, Any]] = []
    for (y, m) in months_back:
        rows.extend(fetch_monthly_kline(
            ticker, y, m, session=sess, logger=log,
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
    root: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
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


def load_kline_from_db(
    ticker: str,
    *,
    months: Optional[int] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    root: Optional[Path] = None,
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
    periods: List[int] = (5, 10, 20, 60, 120),
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
    k_list: List[float] = []
    d_list: List[float] = []
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
    ma_periods: List[int] = (5, 10, 20, 60, 120),
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
    bullish: Optional[bool]
    detail: str


def derive_signals(df_with_indicators: pd.DataFrame) -> List[TechnicalSignal]:
    """依最新一根 K 線判讀常見訊號。"""
    if df_with_indicators is None or df_with_indicators.empty:
        return []
    last = df_with_indicators.iloc[-1]
    prev = df_with_indicators.iloc[-2] if len(df_with_indicators) >= 2 else last
    signals: List[TechnicalSignal] = []

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
    rsi14: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    k: Optional[float] = None
    d: Optional[float] = None
    ma5: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    ma120: Optional[float] = None
    volume_last: Optional[float] = None
    vol_ma20: Optional[float] = None
    boll_upper: Optional[float] = None
    boll_lower: Optional[float] = None
    signals: List[Dict[str, Any]] = field(default_factory=list)
    rows: int = 0
    technical_score: float = 50.0   # 0-100 (給 scoring.py 用)

    @property
    def has_data(self) -> bool:
        return self.rows > 0


def build_technical_snapshot(
    ticker: str,
    *,
    months: int = 6,
    root: Optional[Path] = None,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
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
        rows=int(len(df_ind)),
    )
    sigs = derive_signals(df_ind)
    snap.signals = [asdict(s) for s in sigs]
    snap.technical_score = _score_from_signals(sigs, snap)
    return snap, df_ind


def _pct_change(df: pd.DataFrame, n: int) -> float:
    if len(df) <= n:
        return 0.0
    last_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-1 - n])
    if prev_close <= 0:
        return 0.0
    return round(100.0 * (last_close - prev_close) / prev_close, 2)


def _safe_float(x: Any) -> Optional[float]:
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
    signals: List[TechnicalSignal],
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


def snapshot_to_dict(s: TechnicalSnapshot) -> Dict[str, Any]:
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
    "fetch_monthly_kline",
    "fetch_recent_kline",
    "load_kline_from_db",
    "snapshot_to_dict",
]

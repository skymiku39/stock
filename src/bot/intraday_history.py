"""intraday_history -- 透過 Shioaji 補齊分 K / Tick 歷史並寫入 SQLite。

資料來源
========
* ``api.kbars``  — 1 分 K (建議用於當沖回測)
* ``api.ticks``  — 逐筆成交 (資料量大，建議單日抓取)

儲存
====
* SQLite ``intraday_bars`` table
* 選用 CSV 快取 ``data/intraday/<symbol>/<interval>_<date>.csv``
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from functools import wraps
from pathlib import Path
from typing import Any, Iterable, List, Literal, Optional, Sequence, Tuple

import pandas as pd

from bot.cloud_file_cache import mirror_file_to_cloud
from bot.source_registry import SourceRegistry
from bot.stock_db import IntradayBar, StockDB, default_db_path
from bot.utils import get_logger, mk_folder, now_tw

IntervalKind = Literal["1m", "tick"]
TW_TZ = "Asia/Taipei"


def shioaji_ts_to_iso(ts_ns: int) -> str:
    """Shioaji Kbars/Ticks 的 ns timestamp → 盤中時間字串。

    Shioaji 回傳的 ns 以 UTC 解析後的「鐘面時刻」即台股當地時間
    （勿再 tz_convert +8，否則會變成 17:00 而非 09:00）。
    """
    ts = pd.to_datetime(int(ts_ns), unit="ns", utc=True)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def intraday_ts_to_datetime(ts_str: str) -> dt.datetime:
    """把 DB/CSV 的 ts 字串轉為 datetime，並修正舊版 +8h 偏移資料。"""
    d = dt.datetime.strptime(str(ts_str)[:19], "%Y-%m-%d %H:%M:%S")
    if 14 <= d.hour <= 23:
        d -= dt.timedelta(hours=8)
    return d


def kbars_to_bars(symbol: str, kbars: Any) -> List[IntradayBar]:
    """把 Shioaji Kbars 物件轉成 IntradayBar list。"""
    if kbars is None:
        return []
    data = kbars.model_dump() if hasattr(kbars, "model_dump") else dict(kbars)
    ts_list = data.get("ts") or []
    if not ts_list:
        return []
    n = len(ts_list)
    opens = data.get("Open") or [0.0] * n
    highs = data.get("High") or [0.0] * n
    lows = data.get("Low") or [0.0] * n
    closes = data.get("Close") or [0.0] * n
    volumes = data.get("Volume") or [0] * n
    amounts = data.get("Amount") or [0.0] * n
    out: List[IntradayBar] = []
    for i in range(n):
        out.append(IntradayBar(
            symbol=symbol,
            ts=shioaji_ts_to_iso(ts_list[i]),
            interval="1m",
            open=float(opens[i]),
            high=float(highs[i]),
            low=float(lows[i]),
            close=float(closes[i]),
            volume=float(volumes[i]),
            amount=float(amounts[i]),
            source="shioaji",
        ))
    return out


def ticks_to_bars(symbol: str, ticks: Any) -> List[IntradayBar]:
    """把 Shioaji Ticks 物件轉成 IntradayBar list (interval=tick)。"""
    if ticks is None:
        return []
    data = ticks.model_dump() if hasattr(ticks, "model_dump") else dict(ticks)
    ts_list = data.get("ts") or []
    if not ts_list:
        return []
    closes = data.get("close") or []
    volumes = data.get("volume") or []
    out: List[IntradayBar] = []
    for i, ts_ns in enumerate(ts_list):
        px = float(closes[i]) if i < len(closes) else 0.0
        vol = float(volumes[i]) if i < len(volumes) else 0.0
        out.append(IntradayBar(
            symbol=symbol,
            ts=shioaji_ts_to_iso(ts_ns),
            interval="tick",
            open=px,
            high=px,
            low=px,
            close=px,
            volume=vol,
            amount=px * vol,
            source="shioaji",
        ))
    return out


def iter_trading_days(start: dt.date, end: dt.date) -> List[dt.date]:
    """簡易交易日列舉 (週一至週五)；不含國定假日。"""
    days: List[dt.date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += dt.timedelta(days=1)
    return days


def _cache_path(
    symbol: str,
    interval: IntervalKind,
    day: dt.date,
    root: Optional[Path],
) -> Path:
    base = (root or Path.cwd()) / "data" / "intraday" / symbol
    mk_folder(str(base))
    return base / f"{interval}_{day.isoformat()}.csv"


def save_bars_csv(bars: Sequence[IntradayBar], path: Path) -> None:
    if not bars:
        return
    rows = [
        {
            "symbol": b.symbol,
            "ts": b.ts,
            "interval": b.interval,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "amount": b.amount,
            "source": b.source,
            "source_url": b.source_url,
            "source_date": b.source_date,
            "retrieved_at": b.retrieved_at,
            "quality_status": b.quality_status,
            "source_record_id": b.source_record_id,
        }
        for b in bars
    ]
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _capture_intraday_chunk(
    *,
    root: Optional[Path],
    symbol: str,
    start: dt.date,
    end: dt.date,
    interval: IntervalKind,
    bars: Sequence[IntradayBar],
    db: StockDB,
    logger: logging.Logger,
) -> tuple[str, str, str]:
    """保存 Shioaji 轉換後的分 K/Tick 封套，並明確標成 cache-only。

    Shioaji SDK 已把回應轉成 model，這裡沒有宣稱取得 SDK 底層 HTTP bytes；
    封套的用途是跨機器重播、hash 驗證與 canonical row 的 exact identity，
    provenance 則仍標示為 ``cache_only``。
    """
    if root is None or not bars:
        return "", "", ""
    retrieved_at = now_tw().isoformat(timespec="seconds")
    source_url = f"shioaji://{interval}/{symbol}"
    payload = {
        "schema_version": 1,
        "adapter": "shioaji",
        "symbol": symbol,
        "interval": interval,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "retrieved_at": retrieved_at,
        "rows": [
            {
                "symbol": bar.symbol,
                "ts": bar.ts,
                "interval": bar.interval,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "amount": bar.amount,
            }
            for bar in bars
        ],
    }
    try:
        document = SourceRegistry(root, db=db).capture_document(
            f"shioaji_{interval}",
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            source_url=source_url,
            source_ref=f"{symbol}:{interval}:{start.isoformat()}:{end.isoformat()}",
            title=f"Shioaji {interval} {symbol} {start.isoformat()}~{end.isoformat()}",
            kind="normalized_response",
            suffix="json",
            mime_type="application/json",
            as_of=start.isoformat(),
            retrieved_at=retrieved_at,
            quality_status="partial",
            metadata={
                "adapter": "shioaji",
                "provenance_status": "cache_only",
                "normalized": True,
                "interval": interval,
                "row_count": len(bars),
            },
        )
        return document.document_id, source_url, retrieved_at
    except Exception as exc:  # pragma: no cover - filesystem/runtime boundary
        logger.warning("%s %s raw 封套保存失敗：%s", symbol, interval, exc)
        return "", source_url, retrieved_at


def fetch_intraday_chunk(
    broker: Any,
    symbol: str,
    start: dt.date,
    end: dt.date,
    *,
    interval: IntervalKind = "1m",
    timeout_ms: int = 60_000,
    logger: Optional[logging.Logger] = None,
) -> List[IntradayBar]:
    """抓取單段日期區間 (建議 ≤ 5 個交易日)。"""
    log = logger or get_logger("intraday-history")
    start_s = start.isoformat()
    end_s = end.isoformat()
    if interval == "tick":
        raw = broker.fetch_ticks(symbol, start_s, end_s, timeout_ms=timeout_ms)
        bars = ticks_to_bars(symbol, raw)
    else:
        raw = broker.fetch_kbars(symbol, start_s, end_s, timeout_ms=timeout_ms)
        bars = kbars_to_bars(symbol, raw)
    log.info(
        "%s %s %s~%s: %d 筆",
        symbol, interval, start_s, end_s, len(bars),
    )
    return bars


def _fetch_and_store_intraday_open_db(
    broker: Any,
    symbol: str,
    start: dt.date,
    end: dt.date,
    *,
    interval: IntervalKind = "1m",
    chunk_days: int = 1,
    skip_existing_days: bool = True,
    save_csv: bool = True,
    root: Optional[Path] = None,
    db: Optional[StockDB] = None,
    request_delay_sec: float = 0.5,
    timeout_ms: int = 60_000,
    logger: Optional[logging.Logger] = None,
) -> Tuple[int, int]:
    """分批抓取並寫入 DB；回傳 (寫入筆數, 跳過天數)。"""
    log = logger or get_logger("intraday-history")
    database = db or StockDB.open(path=default_db_path(root))
    trading_days = iter_trading_days(start, end)
    if not trading_days:
        return 0, 0

    chunk_days = max(1, int(chunk_days))
    total_written = 0
    skipped_days = 0
    i = 0
    while i < len(trading_days):
        chunk = trading_days[i:i + chunk_days]
        chunk_start = chunk[0]
        chunk_end = chunk[-1]

        if skip_existing_days and interval == "1m" and chunk_days == 1:
            day = chunk_start
            existing = database.count_intraday_bars(
                symbol,
                interval=interval,
                start=day.isoformat(),
                end=day.isoformat(),
            )
            if existing >= 200:
                log.debug("%s %s 已有 %d 筆，跳過", symbol, day, existing)
                skipped_days += 1
                i += chunk_days
                continue

        try:
            bars = fetch_intraday_chunk(
                broker,
                symbol,
                chunk_start,
                chunk_end,
                interval=interval,
                timeout_ms=timeout_ms,
                logger=log,
            )
        except Exception:
            log.exception(
                "抓取失敗 %s %s %s~%s",
                symbol, interval, chunk_start, chunk_end,
            )
            i += chunk_days
            if request_delay_sec > 0:
                time.sleep(request_delay_sec)
            continue

        if bars:
            source_document_id, source_url, retrieved_at = _capture_intraday_chunk(
                root=root,
                symbol=symbol,
                start=chunk_start,
                end=chunk_end,
                interval=interval,
                bars=bars,
                db=database,
                logger=log,
            )
            source_date = f"{chunk_start.isoformat()}:{chunk_end.isoformat()}"
            for bar in bars:
                bar.source_url = source_url
                bar.source_date = source_date
                bar.retrieved_at = retrieved_at
                bar.quality_status = "partial"
                bar.source_record_id = source_document_id
            total_written += database.bulk_upsert_intraday_bars(bars)
            if save_csv and chunk_days == 1:
                csv_path = _cache_path(symbol, interval, chunk_start, root)
                save_bars_csv(bars, csv_path)
                mirror_file_to_cloud(csv_path, root=root)

        i += chunk_days
        if request_delay_sec > 0:
            time.sleep(request_delay_sec)

    return total_written, skipped_days


@wraps(_fetch_and_store_intraday_open_db)
def fetch_and_store_intraday(*args: Any, **kwargs: Any) -> Tuple[int, int]:
    """抓取並保存盤中資料，明確管理函式自有的 SQLite 連線。

    呼叫端若傳入 ``db``，連線 ownership 仍歸呼叫端；未傳入時由本函式
    建立並在成功、提前 return 或例外後關閉，避免 CLI／排程重複執行時
    累積 SQLite/WAL 資源。
    """
    if kwargs.get("db") is not None:
        return _fetch_and_store_intraday_open_db(*args, **kwargs)

    root = kwargs.get("root")
    database = StockDB.open(path=default_db_path(root))
    kwargs["db"] = database
    try:
        return _fetch_and_store_intraday_open_db(*args, **kwargs)
    finally:
        database.close()


def bars_to_dataframe(bars: Sequence[IntradayBar]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame()
    return pd.DataFrame([
        {
            "symbol": b.symbol,
            "ts": b.ts,
            "interval": b.interval,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "amount": b.amount,
            "source": b.source,
            "source_url": b.source_url,
            "source_date": b.source_date,
            "retrieved_at": b.retrieved_at,
            "quality_status": b.quality_status,
            "source_record_id": b.source_record_id,
        }
        for b in bars
    ])

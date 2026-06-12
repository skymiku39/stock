"""price_band_heat -- 價格區間內現貨成交熱度排行 (TWSE MIS 全市場掃描).

用於觀察「約 10 元」等價格帶中，哪些標的成交量排名靠前。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from bot.market_meta import is_etf, load_market_map
from bot.market_movers import (
    MIS_API_URL,
    MIS_INDEX_URL,
    MoverRow,
    _ex_ch_for_batch,
    _parse_mis_items,
    _session,
    list_scan_tickers,
)
from bot.utils import get_logger, now_tw

DEFAULT_PRICE_LOW = 9.0
DEFAULT_PRICE_HIGH = 12.5

# 先前建議的 ~10 元監控清單（供儀表板標記）
DEFAULT_WATCH_SYMBOLS = (
    "2538", "1464", "1517", "1805", "2104", "1506",
    "9934", "5521", "1316", "1313", "2323", "2547",
)


@dataclass
class PriceBandHeatRow:
    ticker: str
    name: str = ""
    price: Optional[float] = None
    pct_chg: Optional[float] = None
    volume: int = 0
    band_rank: int = 0
    market_volume_rank: int = 0
    tick2_pct: Optional[float] = None
    quote_time: str = ""
    in_watchlist: bool = False


@dataclass
class PriceBandHeatResult:
    asof: str
    price_low: float
    price_high: float
    scanned: int = 0
    quoted: int = 0
    band_count: int = 0
    batch_count: int = 0
    duration_sec: float = 0.0
    rows: List[PriceBandHeatRow] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            **{k: v for k, v in asdict(self).items() if k != "rows"},
            "rows": [asdict(r) for r in self.rows],
        }


def _tick2_pct(price: float) -> float:
    tick = 0.05 if price >= 10 else 0.01
    return round(tick / price * 100 * 2, 2) if price > 0 else 0.0


def _is_dr_like(row: MoverRow) -> bool:
    name = (row.name or "").upper()
    if "DR" in name or "存託" in (row.name or ""):
        return True
    return row.ticker.startswith("91") and len(row.ticker) == 4


def _scan_all_quotes(
    *,
    root: Optional[Path],
    exclude_etf: bool,
    batch_size: int,
    batch_pause_sec: float,
    sess: requests.Session,
    logger: logging.Logger,
) -> tuple[Dict[str, MoverRow], int, int, List[str]]:
    market_map = load_market_map(root=root)
    tickers = list_scan_tickers(root=root, exclude_etf=exclude_etf)
    errors: List[str] = []
    if not tickers:
        return {}, 0, 0, ["無可掃描代號 (market_map 為空)"]

    try:
        sess.get(MIS_INDEX_URL, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return {}, len(tickers), 0, [f"MIS session 初始化失敗: {exc}"]

    merged: Dict[str, MoverRow] = {}
    batches = [tickers[i: i + batch_size] for i in range(0, len(tickers), batch_size)]
    for idx, batch in enumerate(batches):
        ex_ch = _ex_ch_for_batch(batch, market_map)
        try:
            resp = sess.get(
                MIS_API_URL,
                params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
                timeout=20,
            )
            merged.update(_parse_mis_items(resp.json().get("msgArray") or []))
        except Exception as exc:  # noqa: BLE001
            msg = f"批次 {idx + 1}/{len(batches)} 失敗: {exc}"
            logger.warning(msg)
            errors.append(msg)
        if idx + 1 < len(batches) and batch_pause_sec > 0:
            time.sleep(batch_pause_sec)
    return merged, len(tickers), len(batches), errors


def fetch_price_band_heat(
    *,
    root: Optional[Path] = None,
    price_low: float = DEFAULT_PRICE_LOW,
    price_high: float = DEFAULT_PRICE_HIGH,
    limit: int = 50,
    exclude_etf: bool = True,
    exclude_dr: bool = True,
    watch_symbols: Optional[List[str]] = None,
    batch_size: int = 45,
    batch_pause_sec: float = 0.12,
    session: Optional[requests.Session] = None,
    logger: Optional[logging.Logger] = None,
) -> PriceBandHeatResult:
    """掃描全市場，篩選價格區間並依成交量排名。"""
    log = logger or get_logger("price-band-heat")
    t0 = time.time()
    low = min(price_low, price_high)
    high = max(price_low, price_high)
    watch_set = set(watch_symbols or DEFAULT_WATCH_SYMBOLS)

    result = PriceBandHeatResult(
        asof=now_tw().isoformat(timespec="seconds"),
        price_low=low,
        price_high=high,
    )
    sess = session or _session()
    merged, scanned, batch_count, errors = _scan_all_quotes(
        root=root,
        exclude_etf=exclude_etf,
        batch_size=batch_size,
        batch_pause_sec=batch_pause_sec,
        sess=sess,
        logger=log,
    )
    result.scanned = scanned
    result.batch_count = batch_count
    result.errors.extend(errors)
    result.quoted = sum(1 for r in merged.values() if r.price is not None)

    by_volume = sorted(
        [r for r in merged.values() if r.volume > 0 and r.price is not None],
        key=lambda r: r.volume,
        reverse=True,
    )
    market_rank: Dict[str, int] = {
        row.ticker: idx + 1 for idx, row in enumerate(by_volume)
    }

    band_rows: List[PriceBandHeatRow] = []
    for row in merged.values():
        if row.price is None or not (low <= row.price <= high):
            continue
        if exclude_etf and is_etf(row.ticker):
            continue
        if exclude_dr and _is_dr_like(row):
            continue
        band_rows.append(PriceBandHeatRow(
            ticker=row.ticker,
            name=row.name,
            price=row.price,
            pct_chg=row.pct_chg,
            volume=row.volume,
            market_volume_rank=market_rank.get(row.ticker, 0),
            tick2_pct=_tick2_pct(row.price),
            quote_time=row.quote_time,
            in_watchlist=row.ticker in watch_set,
        ))

    band_rows.sort(key=lambda r: r.volume, reverse=True)
    for idx, row in enumerate(band_rows):
        row.band_rank = idx + 1

    result.band_count = len(band_rows)
    result.rows = band_rows[: max(1, limit)]
    result.duration_sec = round(time.time() - t0, 2)
    log.info(
        "price band %.1f-%.1f: scanned=%d band=%d -> top %d (%.1fs)",
        low,
        high,
        result.scanned,
        result.band_count,
        len(result.rows),
        result.duration_sec,
    )
    return result


__all__ = [
    "DEFAULT_PRICE_HIGH",
    "DEFAULT_PRICE_LOW",
    "DEFAULT_WATCH_SYMBOLS",
    "PriceBandHeatResult",
    "PriceBandHeatRow",
    "fetch_price_band_heat",
]

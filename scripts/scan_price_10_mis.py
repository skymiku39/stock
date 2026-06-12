"""Scan TWSE MIS for stocks around 10 TWD (8-13), liquid, non-ETF."""
from __future__ import annotations

import time
from pathlib import Path

from bot.market_meta import is_etf, load_market_map
from bot.market_movers import (
    MIS_API_URL,
    MIS_INDEX_URL,
    _ex_ch_for_batch,
    _parse_mis_items,
    _session,
    list_scan_tickers,
)


def scan(
    *,
    low: float = 9.0,
    high: float = 12.5,
    min_volume: int = 0,
    batch_size: int = 45,
) -> list[dict]:
    root = Path.cwd()
    market_map = load_market_map(root=root)
    tickers = list_scan_tickers(root=root, exclude_etf=True)
    sess = _session()
    sess.get(MIS_INDEX_URL, timeout=15)
    merged: dict = {}
    batches = [tickers[i: i + batch_size] for i in range(0, len(tickers), batch_size)]
    for idx, batch in enumerate(batches):
        ex_ch = _ex_ch_for_batch(batch, market_map)
        try:
            resp = sess.get(
                MIS_API_URL,
                params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
                timeout=25,
            )
            merged.update(_parse_mis_items(resp.json().get("msgArray") or []))
        except Exception as exc:
            print(f"batch {idx+1} fail: {exc}")
        time.sleep(0.12)

    rows: list[dict] = []
    for code, row in merged.items():
        if is_etf(code):
            continue
        if row.price is None or not (low <= row.price <= high):
            continue
        if min_volume > 0 and row.volume < min_volume:
            continue
        close = row.price
        # tick: 10-50 uses 0.05
        tick = 0.05 if close >= 10 else 0.02 if close < 10 else 0.05
        tick2_pct = tick / close * 100 * 2
        rows.append({
            "symbol": code,
            "name": row.name,
            "price": close,
            "volume": row.volume,
            "pct_chg": row.pct_chg,
            "tick2_pct": round(tick2_pct, 2),
        })
    rows.sort(key=lambda x: x["volume"], reverse=True)
    return rows


if __name__ == "__main__":
    found = scan()
    print(f"scanned_ok={len(found)}")
    for r in found[:40]:
        print(
            f"{r['symbol']:6} {(r['name'] or '')[:10]:10} "
            f"px={r['price']:6.2f} vol={r['volume']:>9} "
            f"chg={r['pct_chg'] if r['pct_chg'] is not None else 0:>6.2f}% "
            f"2tick~{r['tick2_pct']:.2f}%"
        )

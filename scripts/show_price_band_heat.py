"""Print ~10 yuan price band heat rankings."""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot.price_band_heat import DEFAULT_WATCH_SYMBOLS, fetch_price_band_heat

r = fetch_price_band_heat(
    root=Path("."),
    price_low=9.0,
    price_high=12.5,
    limit=100,
    exclude_dr=True,
)
print(f"band_count={r.band_count} scanned={r.scanned}")
print("--- TOP 15 ---")
for row in r.rows[:15]:
    star = "*" if row.in_watchlist else " "
    print(
        f"{row.band_rank:3}{star} {row.ticker} {(row.name or '')[:8]:8} "
        f"px={row.price:6.2f} vol={row.volume:>7} "
        f"mkt={row.market_volume_rank:>4} chg={row.pct_chg or 0:+.2f}%"
    )
print("--- WATCHLIST ---")
by_sym = {row.ticker: row for row in r.rows}
for sym in DEFAULT_WATCH_SYMBOLS:
    row = by_sym.get(sym)
    if row:
        print(
            f"{row.band_rank:3}/{r.band_count} {sym} {(row.name or '')[:8]:8} "
            f"vol={row.volume:>7} mkt_rank={row.market_volume_rank}"
        )
    else:
        print(f"  - {sym} not in band")

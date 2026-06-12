"""Scan stocks around 10 TWD with liquidity."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "stock.db"

SQL = """
WITH latest AS (
  SELECT ph.symbol, ph.date, ph.close, ph.volume,
         ROW_NUMBER() OVER (PARTITION BY ph.symbol ORDER BY ph.date DESC) AS rn
  FROM price_history ph
),
avg_vol AS (
  SELECT symbol, AVG(volume) AS avg_vol
  FROM price_history
  WHERE date >= date('now', '-30 day')
  GROUP BY symbol
),
volatility AS (
  SELECT symbol,
         AVG((high - low) / NULLIF(close, 0) * 100) AS avg_range_pct
  FROM price_history
  WHERE date >= date('now', '-20 day')
  GROUP BY symbol
)
SELECT l.symbol, si.name, si.market, l.close, l.volume AS last_vol,
       av.avg_vol, v.avg_range_pct, l.date
FROM latest l
LEFT JOIN stock_info si ON si.symbol = l.symbol
LEFT JOIN avg_vol av ON av.symbol = l.symbol
LEFT JOIN volatility v ON v.symbol = l.symbol
WHERE l.rn = 1
  AND l.close BETWEEN 8.0 AND 13.0
  AND LENGTH(l.symbol) = 4
  AND l.symbol GLOB '[0-9][0-9][0-9][0-9]'
  AND COALESCE(av.avg_vol, 0) >= 300000
ORDER BY av.avg_vol DESC
"""


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(SQL).fetchall()
    print(f"matched={len(rows)}")
    for r in rows[:60]:
        close = float(r["close"])
        tick2_pct = 0.05 / close * 100 * 2 if close >= 10 else 0.02 / close * 100 * 2
        name = (r["name"] or "")[:14]
        print(
            f"{r['symbol']:6} {name:14} {close:6.2f} "
            f"vol={int(r['avg_vol'] or 0):>9} "
            f"range20d={float(r['avg_range_pct'] or 0):4.1f}% "
            f"2tick~{tick2_pct:.2f}%"
        )


if __name__ == "__main__":
    main()

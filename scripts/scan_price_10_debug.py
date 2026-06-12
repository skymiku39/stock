from pathlib import Path
import time
from bot.market_meta import load_market_map
from bot.market_movers import MIS_INDEX_URL, MIS_API_URL, _ex_ch_for_batch, _parse_mis_items, _session, list_scan_tickers

root = Path.cwd()
tickers = list_scan_tickers(root=root)[:90]
market_map = load_market_map(root=root)
sess = _session()
sess.get(MIS_INDEX_URL, timeout=15)
ex_ch = _ex_ch_for_batch(tickers, market_map)
resp = sess.get(MIS_API_URL, params={"ex_ch": ex_ch, "json": "1", "delay": "0"}, timeout=25)
items = resp.json().get("msgArray") or []
merged = _parse_mis_items(items)
print("parsed", len(merged))
prices = [r.price for r in merged.values() if r.price]
print("with price", len(prices), "sample", sorted(prices)[:10], "...", sorted(prices)[-5:] if prices else [])
near = [r for r in merged.values() if r.price and 8<=r.price<=13]
print("near10", len(near))
for r in near[:15]:
    print(r.ticker, r.name[:8], r.price, r.volume)

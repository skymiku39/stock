"""Sample Taifex Hot Stock Futures getQuoteList response."""
from __future__ import annotations

import json
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://mis.taifex.com.tw/futures/api/getQuoteList"
page_attr = {
    "MarketType": "0",
    "SymbolType": "F",
    "KindID": "4",
    "Hot": "T",
    "CID": "",
    "ExpireMonth": "",
    "SortColumn": "CTotalVolume",
    "AscDesc": "D",
}
r = requests.post(
    API,
    json=page_attr,
    headers={
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://mis.taifex.com.tw",
        "Referer": "https://mis.taifex.com.tw/futures/RegularSession/StockProducts/HotStockFutures/",
        "Content-Type": "application/json",
    },
    timeout=30,
)
data = r.json()
items = data.get("RtData", {}).get("QuoteList", [])
print("count", len(items))
if items:
    print("keys", sorted(items[0].keys()))
    for row in items[:5]:
        print(json.dumps(row, ensure_ascii=False)[:400])

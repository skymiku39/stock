"""Find POST endpoint names in Taifex MIS API."""
from __future__ import annotations

import json
import re
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

t = requests.get(
    "https://mis.taifex.com.tw/futures/_nuxt/cd35741.js",
    timeout=60,
).text

# axios.post first arg patterns
for m in re.finditer(r'\.post\(([^,)]+)', t):
    arg = m.group(1).strip().strip('"').strip("'")
    if len(arg) < 80 and ("/" in arg or "get" in arg.lower() or "query" in arg.lower() or "Quote" in arg):
        print("post", arg[:100])

# common endpoint string literals
for m in re.finditer(r'"([A-Za-z][A-Za-z0-9_/]{4,50})"', t):
    s = m.group(1)
    if any(k in s.lower() for k in ("quote", "symbol", "product", "market", "snapshot", "list")):
        if s.count("/") <= 2:
            print("str", s)

# try probing known patterns
API = "https://mis.taifex.com.tw/futures/api/"
s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://mis.taifex.com.tw",
    "Referer": "https://mis.taifex.com.tw/futures/RegularSession/StockProducts/HotStockFutures/",
    "Content-Type": "application/json",
})
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
payloads = [
    ("getQuoteList", page_attr),
    ("getQuote", page_attr),
    ("getProductList", page_attr),
    ("getSymbolList", page_attr),
    ("queryQuote", page_attr),
    ("queryQuoteList", page_attr),
    ("getMarketData", page_attr),
    ("getSnapshot", page_attr),
    ("getPageData", {"Level1ID": "0", "pageAttr": page_attr}),
    ("getPageQuote", {"Level1ID": "0", "pageAttr": page_attr}),
]
for ep, body in payloads:
    try:
        r = s.post(API + ep, json=body, timeout=15)
        print(ep, r.status_code, r.text[:180].replace("\n", " "))
    except Exception as exc:
        print(ep, "ERR", exc)

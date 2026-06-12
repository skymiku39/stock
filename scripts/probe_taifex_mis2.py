"""Search Taifex MIS nuxt bundles for API paths."""
from __future__ import annotations

import re

import requests

BASE = "https://mis.taifex.com.tw/futures/"
s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"
html = s.get(BASE + "RegularSession/StockProducts/HotStockFutures/", timeout=20).text
js_files = re.findall(r'src="(/futures/_nuxt/[^"]+\.js)"', html)
print("js files", js_files)
keywords = ("HotStock", "StockFutures", "rtCore", "/api/", "getQuote", "StockProducts")
for js in js_files:
    text = s.get("https://mis.taifex.com.tw" + js, timeout=30).text
    print("\n===", js, "len", len(text))
    for kw in keywords:
        idx = 0
        found = 0
        while found < 5:
            i = text.find(kw, idx)
            if i < 0:
                break
            print(kw, "...", text[max(0, i - 40) : i + 80].replace("\n", " "))
            idx = i + len(kw)
            found += 1
    # extract path-like strings
    paths = set(re.findall(r'["\'](/futures/[^"\']{5,80})["\']', text))
    for p in sorted(paths)[:30]:
        if "api" in p.lower() or "quote" in p.lower() or "stock" in p.lower():
            print("path", p)

"""Find Taifex MIS data fetch URLs in nuxt bundle."""
from __future__ import annotations

import re
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

t = requests.get(
    "https://mis.taifex.com.tw/futures/_nuxt/ab9396b.js",
    timeout=60,
).text
keys = [
    "mRegSymbols",
    "KindID",
    "pageAttr",
    "CTotalVolume",
    "SymbolType",
    "getSnapshot",
    "getData",
    "queryQuote",
    "queryData",
    "rtCore",
]
for k in keys:
    print(k, t.count(k))

for m in re.finditer("mRegSymbols", t):
    print("mRegSymbols ctx:", t[m.start() - 80 : m.start() + 220].replace("\n", " ")[:280])
    break

paths = set(re.findall(r'["\'](/futures/[a-zA-Z0-9_/]+)["\']', t))
for p in sorted(paths):
    print("path", p)

# axios/fetch URLs
for m in re.finditer(r'baseURL[^;]{0,200}', t):
    print("baseURL", m.group(0)[:180])

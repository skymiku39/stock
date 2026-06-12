"""Deep search cd35741.js for Taifex MIS HTTP endpoints."""
from __future__ import annotations

import re
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

t = requests.get(
    "https://mis.taifex.com.tw/futures/_nuxt/cd35741.js",
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
    "axios",
    "fetch(",
    "$axios",
    "rtCore",
    "SockJS",
    "send(",
    "RegSymbols",
]
for k in keys:
    print(k, t.count(k))

for m in re.finditer("mRegSymbols", t):
    print("mRegSymbols:", t[m.start() - 100 : m.start() + 280].replace("\n", " ")[:360])
    if m.start() > 50000:
        break

paths = set(re.findall(r'["\'](/futures/[a-zA-Z0-9_/]+)["\']', t))
for p in sorted(paths):
    print("path", p)

# look for http paths without /futures prefix
for m in re.finditer(r'["\'](/[a-zA-Z][a-zA-Z0-9_/]{3,60})["\']', t):
    p = m.group(1)
    if any(x in p.lower() for x in ("quote", "data", "api", "snap", "symbol", "core")):
        print("alt", p)

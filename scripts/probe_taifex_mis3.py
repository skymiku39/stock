"""Extract Taifex MIS REST/WS endpoints from nuxt bundle."""
from __future__ import annotations

import re
import sys

import requests

s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"
text = s.get("https://mis.taifex.com.tw/futures/_nuxt/ab9396b.js", timeout=60).text
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

patterns = [
    r'["\']([^"\']*api[^"\']*)["\']',
    r'["\']([^"\']*Quote[^"\']*)["\']',
    r'["\']([^"\']*rtCore[^"\']*)["\']',
    r'["\']([^"\']*StockFutures[^"\']*)["\']',
    r'https://mis\.taifex\.com\.tw[^"\']+',
]
seen: set[str] = set()
for pat in patterns:
    for m in re.findall(pat, text, flags=re.I):
        if len(m) < 4 or m in seen:
            continue
        seen.add(m)
        if any(k in m.lower() for k in ("api", "quote", "rtcore", "stock", "futures", "mis.taifex")):
            print(m[:160])

# search subscribe channel names
for m in re.findall(r"subscribe[^;]{0,200}", text, flags=re.I)[:20]:
    print("SUB", m[:180])

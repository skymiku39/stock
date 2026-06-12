"""Probe Taifex MIS SockJS rtCore for Hot Stock Futures."""
from __future__ import annotations

import json
import re
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://mis.taifex.com.tw/futures"
s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://mis.taifex.com.tw",
    "Referer": f"{BASE}/RegularSession/StockProducts/HotStockFutures/",
})

info = s.get(f"{BASE}/rtCore/info", params={"t": int(time.time() * 1000)}, timeout=15)
print("info", info.status_code, info.text[:300])
info_json = info.json()
print("info_json", info_json)

# download page chunk with HotStock logic
text = s.get(f"{BASE}/_nuxt/cd35741.js", timeout=60).text
# find Level1ID near HotStockFutures
for m in re.finditer("HotStockFutures", text):
    chunk = text[m.start() : m.start() + 600]
    if "Level1ID" in chunk:
        print("CHUNK", chunk[:500])
        break

# search send/subscribe patterns
for pat in [r"send\([^)]{0,120}\)", r"subscribe[^;]{0,150}", r"Level1ID[^,]{0,80}"]:
    ms = re.findall(pat, text)
    print("pat", pat, "count", len(ms))
    for x in ms[:5]:
        print(" ", x[:140])

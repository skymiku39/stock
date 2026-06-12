"""Find axios calls in cd35741.js."""
from __future__ import annotations

import re
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

t = requests.get(
    "https://mis.taifex.com.tw/futures/_nuxt/cd35741.js",
    timeout=60,
).text

for m in re.finditer(r"\$axios\.[a-z]+\([^)]{0,200}\)", t):
    print(m.group(0)[:200])

for m in re.finditer(r'axios\.[a-z]+\([^)]{0,200}\)', t):
    print("ax", m.group(0)[:200])

# strings near "api"
idx = 0
n = 0
while n < 30:
    i = t.find("api", idx)
    if i < 0:
        break
    frag = t[max(0, i - 30) : i + 60]
    if "/" in frag and "api" in frag:
        print("api_frag", frag.replace("\n", " "))
        n += 1
    idx = i + 3

# search doRefreshPage
for m in re.finditer("doRefreshPage", t):
    print("refresh", t[m.start() : m.start() + 500].replace("\n", " ")[:480])
    break

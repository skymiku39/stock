"""Extract /futures/api/* paths from cd35741.js."""
from __future__ import annotations

import re
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

t = requests.get(
    "https://mis.taifex.com.tw/futures/_nuxt/cd35741.js",
    timeout=60,
).text

for m in re.finditer(r"/futures/api/[a-zA-Z0-9_]+", t):
    print(m.group(0))

for m in re.finditer(r'QNameMap', t):
    print("QNameMap ctx:", t[m.start() : m.start() + 800].replace("\n", " ")[:750])
    break

for m in re.finditer(r'gRtCore', t):
    print("gRtCore ctx:", t[m.start() - 50 : m.start() + 400].replace("\n", " ")[:420])
    if m.start() > 100000:
        break

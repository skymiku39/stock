"""Probe Taifex MIS Hot Stock Futures API."""
from __future__ import annotations

import json
import re

import requests

URL = "https://mis.taifex.com.tw/futures/RegularSession/StockProducts/HotStockFutures/"


def main() -> None:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    r = s.get(URL, timeout=20)
    print("status", r.status_code, "len", len(r.text))
    apis = set(re.findall(r"https?://[^\s\"']+", r.text))
    for u in sorted(apis):
        if "api" in u.lower() or "mis" in u.lower() or "taifex" in u.lower():
            print("url", u[:150])
    for m in re.findall(r'src=["\']([^"\']+)["\']', r.text):
        if ".js" in m:
            print("js", m[:120])
    # try common taifex mis api patterns
    candidates = [
        "https://mis.taifex.com.tw/futures/api/getStockFuturesInfo",
        "https://mis.taifex.com.tw/futures/api/getStockFuturesInfo.jsp",
        "https://mis.taifex.com.tw/futures/api/getHotStockFuturesInfo",
        "https://mis.taifex.com.tw/futures/api/getHotStockFuturesInfo.jsp",
        "https://mis.taifex.com.tw/futures/api/getQuote",
        "https://mis.taifex.com.tw/futures/api/getQuote.jsp",
    ]
    s.get("https://mis.taifex.com.tw/futures/", timeout=15)
    for api in candidates:
        try:
            resp = s.get(api, params={"json": "1"}, timeout=15)
            print(api, resp.status_code, resp.text[:200].replace("\n", " "))
        except Exception as exc:
            print(api, "ERR", exc)


if __name__ == "__main__":
    main()

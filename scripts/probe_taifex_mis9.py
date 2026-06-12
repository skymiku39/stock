"""Brute-force Taifex MIS API endpoint discovery."""
from __future__ import annotations

import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
endpoints = [
    "getQuoteList", "getQuote", "getProductList", "getSymbolList",
    "queryQuote", "queryQuoteList", "getMarketData", "getSnapshot",
    "getPageData", "getPageQuote", "getQuotes", "getProducts",
    "queryProducts", "querySymbols", "getHotStockFutures",
    "getRegSymbols", "getSymbolQuote", "queryPageQuote",
    "getLevel1Quote", "getLevel1Data", "getRtQuote",
    "quoteList", "productList", "symbolList",
]
for ep in endpoints:
    for body in (
        page_attr,
        {"Level1ID": "0", "pageAttr": page_attr},
        {"pageAttr": page_attr},
    ):
        try:
            r = s.post(API + ep, json=body, timeout=12)
            if r.status_code != 404:
                print(ep, "body_keys", list(body.keys()), r.status_code, r.text[:250].replace("\n", " "))
        except Exception as exc:
            print(ep, "ERR", exc)

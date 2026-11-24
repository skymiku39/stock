"""Finnhub：美股公司新聞 / 大盤新聞。金鑰走標頭 X-Finnhub-Token。"""
from __future__ import annotations

import datetime as dt
from typing import Any, List, Optional

from bot.free_sources.base import BaseClient, iso_utc, news_item

BASE_URL = "https://finnhub.io/api/v1"


class FinnhubClient(BaseClient):
    source = "finnhub"

    def _headers(self) -> dict:
        return {"X-Finnhub-Token": self._api_key}

    def company_news(self, symbol: str, from_date: str, to_date: str) -> List[dict]:
        params = {"symbol": symbol, "from": str(from_date), "to": str(to_date)}
        return self._get(f"{BASE_URL}/company-news", params, self._headers())

    def general_news(self, min_id: Optional[int] = None) -> List[dict]:
        params: dict = {"category": "general"}
        if min_id is not None:
            params["minId"] = min_id
        return self._get(f"{BASE_URL}/news", params, self._headers())


def normalize_news(rows: Any, symbol: Optional[str] = None) -> List[dict]:
    out: List[dict] = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("headline"):
            continue
        try:
            ts = iso_utc(dt.datetime.fromtimestamp(int(r.get("datetime") or 0), dt.timezone.utc))
        except (TypeError, ValueError, OverflowError, OSError):
            ts = ""
        related = [t.strip().upper() for t in str(r.get("related") or "").split(",") if t.strip()]
        tickers = [symbol.upper()] if symbol else related
        out.append(
            news_item(
                title=str(r["headline"]),
                url=str(r.get("url") or ""),
                published_at=ts,
                source=str(r.get("source") or ""),
                feed=f"finnhub:{symbol.upper()}" if symbol else "finnhub:general",
                summary=str(r.get("summary") or ""),
                us_tickers=tickers,
                category=str(r.get("category") or ""),
                external_id=r.get("id"),
            )
        )
    return out

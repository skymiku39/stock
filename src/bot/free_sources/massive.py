"""Massive（原 Polygon.io）：美股新聞。金鑰走 Authorization: Bearer（翻頁網址不帶金鑰）。"""
from __future__ import annotations

from typing import Any, Iterator, List, Optional
from urllib.parse import urlparse

from bot.free_sources.base import BaseClient, news_item

BASE_URL = "https://api.massive.com"
_HOST = "api.massive.com"


class MassiveClient(BaseClient):
    source = "massive"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    def news(
        self,
        ticker: Optional[str] = None,
        published_gte: Optional[str] = None,
        limit: int = 100,
    ) -> dict:
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if published_gte:
            params["published_utc.gte"] = published_gte
        return self._get(f"{BASE_URL}/v2/reference/news", params, self._headers())

    def news_next(self, next_url: str) -> dict:
        parsed = urlparse(next_url)
        if parsed.scheme != "https" or parsed.hostname != _HOST:
            # 不把金鑰標頭送到其他主機
            raise self._err(None, "refusing next_url outside api.massive.com", "bad_response")
        return self._get(next_url, None, self._headers())

    def iter_news(self, max_pages: int = 3, **kwargs: Any) -> Iterator[dict]:
        page = self.news(**kwargs)
        yield page
        for _ in range(max_pages - 1):
            nxt = page.get("next_url") if isinstance(page, dict) else None
            if not nxt:
                return
            page = self.news_next(nxt)
            yield page


def normalize_news(body: Any) -> List[dict]:
    out: List[dict] = []
    results = body.get("results") if isinstance(body, dict) else None
    for r in results or []:
        if not isinstance(r, dict) or not r.get("title"):
            continue
        publisher = (r.get("publisher") or {}).get("name") or ""
        out.append(
            news_item(
                title=str(r["title"]),
                url=str(r.get("article_url") or ""),
                published_at=str(r.get("published_utc") or ""),
                source=str(publisher),
                feed=f"massive:{publisher}",
                summary=str(r.get("description") or ""),
                us_tickers=[str(t).upper() for t in r.get("tickers") or []],
                insights=[
                    {
                        "ticker": i.get("ticker"),
                        "sentiment": i.get("sentiment"),
                        "reasoning": i.get("sentiment_reasoning"),
                    }
                    for i in r.get("insights") or []
                    if isinstance(i, dict)
                ],
            )
        )
    return out

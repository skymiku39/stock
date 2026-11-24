"""Marketaux：持股相關新聞與情緒分數。金鑰走查詢參數 api_token。"""
from __future__ import annotations

from typing import Any, List, Sequence, Union

from bot.free_sources.base import BaseClient, news_item

URL = "https://api.marketaux.com/v1/news/all"


class MarketauxClient(BaseClient):
    source = "marketaux"

    def news_all(
        self,
        symbols: Union[str, Sequence[str]],
        published_after: str,
        language: str = "en",
        page: int = 1,
    ) -> dict:
        if not isinstance(symbols, str):
            symbols = ",".join(symbols)
        params = {
            "symbols": symbols,
            "filter_entities": "true",
            "language": language,
            "published_after": published_after,
            "api_token": self._api_key,
        }
        if page and page > 1:
            params["page"] = page
        return self._get(URL, params)


def normalize_news(body: Any) -> List[dict]:
    out: List[dict] = []
    rows = body.get("data") if isinstance(body, dict) else None
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("title"):
            continue
        entities = [e for e in r.get("entities") or [] if isinstance(e, dict)]
        scores = [e["sentiment_score"] for e in entities if isinstance(e.get("sentiment_score"), (int, float))]
        src = str(r.get("source") or "")
        out.append(
            news_item(
                title=str(r["title"]),
                url=str(r.get("url") or ""),
                published_at=str(r.get("published_at") or ""),
                source=src,
                feed=f"marketaux:{src}",
                summary=str(r.get("description") or r.get("snippet") or ""),
                us_tickers=[str(e["symbol"]).upper() for e in entities if e.get("symbol")],
                sentiment_score=(sum(scores) / len(scores)) if scores else None,
                entities=[
                    {"symbol": e.get("symbol"), "sentiment_score": e.get("sentiment_score"),
                     "match_score": e.get("match_score")}
                    for e in entities
                ],
            )
        )
    return out

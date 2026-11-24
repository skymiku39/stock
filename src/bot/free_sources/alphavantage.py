"""Alpha Vantage NEWS_SENTIMENT。金鑰走查詢參數 apikey。

HTTP 200 但沒有 feed（Information / Note / Error Message）一律視為錯誤。
time_published 視為 UTC（官方文件未明載時區，使用前以實測確認）。
"""
from __future__ import annotations

import datetime as dt
from typing import Any, List, Union

from bot.free_sources.base import BaseClient, iso_utc, news_item

URL = "https://www.alphavantage.co/query"


class AlphaVantageClient(BaseClient):
    source = "alphavantage"

    def _check_body(self, data: Any) -> None:
        if isinstance(data, dict) and "feed" in data:
            return
        msg = ""
        if isinstance(data, dict):
            msg = str(data.get("Information") or data.get("Note") or data.get("Error Message") or "")
        low = msg.lower()
        if "premium" in low:
            kind = "forbidden"  # 2026-11-24 實測：免費金鑰查 NEWS_SENTIMENT 回 premium endpoint
        elif any(w in low for w in ("rate limit", "per day", "call frequency")):
            kind = "quota_exhausted"
        else:
            kind = "api_error"
        raise self._err(self.last_status, msg or "response has no feed", kind)

    def news_sentiment(
        self, topics: str, time_from: Union[str, dt.datetime], limit: int = 50
    ) -> dict:
        if isinstance(time_from, dt.datetime):
            time_from = time_from.strftime("%Y%m%dT%H%M")
        params = {
            "function": "NEWS_SENTIMENT",
            "topics": topics,
            "time_from": time_from,
            "limit": limit,
            "apikey": self._api_key,
        }
        return self._get(URL, params)


def _num(v: Any) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_news(body: Any) -> List[dict]:
    out: List[dict] = []
    for r in (body.get("feed") if isinstance(body, dict) else None) or []:
        if not isinstance(r, dict) or not r.get("title"):
            continue
        try:
            ts = iso_utc(
                dt.datetime.strptime(str(r.get("time_published")), "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
            )
        except ValueError:
            ts = ""
        ticks = [t for t in r.get("ticker_sentiment") or [] if isinstance(t, dict)]
        src = str(r.get("source") or "")
        out.append(
            news_item(
                title=str(r["title"]),
                url=str(r.get("url") or ""),
                published_at=ts,
                source=src,
                feed=f"alphavantage:{src}",
                summary=str(r.get("summary") or ""),
                us_tickers=[str(t["ticker"]).upper() for t in ticks if t.get("ticker")],
                sentiment_score=_num(r.get("overall_sentiment_score")),
                sentiment_label=r.get("overall_sentiment_label"),
                ticker_sentiment=[
                    {
                        "ticker": t.get("ticker"),
                        "relevance_score": _num(t.get("relevance_score")),
                        "sentiment_score": _num(t.get("ticker_sentiment_score")),
                    }
                    for t in ticks
                ],
            )
        )
    return out

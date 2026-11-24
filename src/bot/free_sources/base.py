"""共用 HTTP 基底：錯誤分類、金鑰遮蔽、用量登記。"""
from __future__ import annotations

import datetime
import re
from typing import Any, Iterable, Mapping, Optional

import requests

from bot.free_sources.quota import QuotaTracker, default_quota

_SECRET_QS = re.compile(
    r"(?i)\b(token|api_?key|api_token|apikey|access_token)=([^&\s'\"]+)"
)
_STATUS_KIND = {
    401: "auth",
    402: "quota_exhausted",
    403: "forbidden",
    404: "not_found",
    429: "rate_limited",
}


def redact(text: object, secrets: Iterable[str] = ()) -> str:
    """遮蔽金鑰：已知金鑰字串與 URL 查詢參數中的 token / apikey。"""
    out = str(text)
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return _SECRET_QS.sub(lambda m: f"{m.group(1)}=***", out)


class SourceError(Exception):
    """資料來源錯誤。kind: auth/quota_exhausted/forbidden/not_found/rate_limited/
    http_error/network/bad_response/api_error/invalid_params/plan_restricted。
    訊息保證已遮蔽金鑰。"""

    def __init__(self, source: str, status: Optional[int], message: str, kind: Optional[str] = None):
        self.source = source
        self.status = status
        self.message = message
        self.kind = kind or _kind_for(status)
        super().__init__(f"{source} [{self.kind}] status={status}: {message}")


def _kind_for(status: Optional[int]) -> str:
    if status is None:
        return "network"
    if status in _STATUS_KIND:
        return _STATUS_KIND[status]
    return "http_error"


class BaseClient:
    source = ""

    def __init__(
        self,
        api_key: str,
        session: Optional[requests.Session] = None,
        quota: Optional[QuotaTracker] = None,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self.session = session if session is not None else requests.Session()
        self.quota = quota if quota is not None else default_quota(self.source)
        self.timeout = timeout
        self.last_status: Optional[int] = None

    def __repr__(self) -> str:  # 避免金鑰經由 repr 外洩
        return f"<{type(self).__name__}>"

    def _err(self, status: Optional[int], message: object, kind: Optional[str] = None) -> SourceError:
        return SourceError(self.source, status, redact(message, [self._api_key])[:300], kind)

    def _check_body(self, data: Any) -> None:
        """子類別覆寫：HTTP 200 但內容其實是錯誤時 raise。"""

    def _classify_http_error(self, status: int, body: str) -> Optional[str]:
        """子類別覆寫：依回應內容細分錯誤種類（例如同為 402，參數錯與額度用完）。
        回傳 None 表示沿用狀態碼的預設分類。"""
        return None

    def _get(
        self,
        url: str,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        self.quota.consume()  # 超限直接 raise QuotaExceeded，不送請求
        self.last_status = None
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise self._err(None, f"{type(exc).__name__}: {exc}", "network") from None
        status = int(resp.status_code)
        self.last_status = status
        if status >= 400:
            body = getattr(resp, "text", "") or ""
            raise self._err(status, body[:200] or f"HTTP {status}", self._classify_http_error(status, body))
        try:
            data = resp.json()
        except ValueError:
            raise self._err(status, "response is not valid JSON", "bad_response") from None
        self._check_body(data)
        return data


def iso_utc(value: "datetime.datetime") -> str:
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def news_item(
    *,
    title: str,
    url: str,
    published_at: str,
    source: str,
    feed: str,
    summary: str,
    us_tickers: list,
    **extra: Any,
) -> dict:
    """共同新聞字典；欄位名對齊 RSS 存檔（title/url/desc/published_at/feed）。"""
    item = {
        "title": title,
        "url": url,
        "desc": summary,
        "summary": summary,
        "published_at": published_at,
        "source": source,
        "feed": feed,
        "region": "us",
        "lang": "en",
        "us_tickers": us_tickers,
    }
    item.update(extra)
    return item

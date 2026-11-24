"""免費資料來源客戶端（離線；假 Session，不打網路）。"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest
import requests

from bot.free_sources import alphavantage, finnhub, fmp, fugle, finmind, marketaux, massive
from bot.free_sources.base import SourceError, redact
from bot.free_sources.probe import run_probe
from bot.free_sources.quota import TAIPEI, QuotaExceeded, QuotaTracker, default_quota

KEY = "SECRET-KEY-123"


class FakeResp:
    def __init__(self, payload=None, status=200, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, *responses, exc=None):
        self.responses = list(responses)
        self.calls = []
        self.exc = exc

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def quota(tmp_path, name, **kw):
    return QuotaTracker(name, directory=tmp_path, **kw)


# ---------------- quota ----------------
def test_quota_daily_blocks_without_sending(tmp_path):
    sess = FakeSession(FakeResp([]))
    c = finnhub.FinnhubClient(KEY, sess, quota(tmp_path, "finnhub", daily_limit=1))
    c.general_news()
    with pytest.raises(QuotaExceeded):
        c.general_news()
    assert len(sess.calls) == 1


def test_quota_resets_on_taiwan_date_and_persists(tmp_path):
    t = [dt.datetime(2026, 11, 24, 23, 0, tzinfo=TAIPEI)]
    q = QuotaTracker("x", daily_limit=1, directory=tmp_path, now=lambda: t[0])
    q.consume()
    assert QuotaTracker("x", daily_limit=1, directory=tmp_path, now=lambda: t[0]).used_today() == 1
    with pytest.raises(QuotaExceeded):
        q.consume()
    t[0] = dt.datetime(2026, 11, 25, 0, 1, tzinfo=TAIPEI)
    q.consume()
    assert (tmp_path / "x.json").exists()


def test_quota_per_minute_and_hour(tmp_path):
    t = [dt.datetime(2026, 11, 24, 10, 0, tzinfo=TAIPEI)]
    q = QuotaTracker("m", per_minute=2, directory=tmp_path, now=lambda: t[0])
    q.consume(); q.consume()
    with pytest.raises(QuotaExceeded):
        q.consume()
    t[0] += dt.timedelta(seconds=61)
    q.consume()
    h = QuotaTracker("h", per_hour=1, directory=tmp_path, now=lambda: t[0])
    h.consume()
    with pytest.raises(QuotaExceeded):
        h.consume()


def test_default_limits(tmp_path):
    assert default_quota("alphavantage", tmp_path).daily_limit == 20
    assert default_quota("marketaux", tmp_path).daily_limit == 90
    assert default_quota("fmp", tmp_path).daily_limit == 230
    assert default_quota("finmind", tmp_path).per_hour == 500


# ---------------- errors / redaction ----------------
def test_redact():
    assert KEY not in redact(f"GET https://x/y?apikey={KEY}&a=1", [KEY])
    assert "api_token=***" in redact("https://x?api_token=abc")
    assert KEY not in redact(f"header {KEY}", [KEY])


@pytest.mark.parametrize(
    "status,kind",
    [(401, "auth"), (402, "quota_exhausted"), (403, "forbidden"), (429, "rate_limited"), (500, "http_error")],
)
def test_error_kinds_and_no_key_leak(tmp_path, status, kind):
    sess = FakeSession(FakeResp(None, status, text=f"bad apikey={KEY} {KEY}"))
    c = fmp.FmpClient(KEY, sess, quota(tmp_path, "fmp"))
    with pytest.raises(SourceError) as ei:
        c.income_statement("AAPL")
    assert ei.value.kind == kind and ei.value.status == status
    assert KEY not in str(ei.value) and KEY not in repr(c)


def test_network_error_redacted(tmp_path):
    sess = FakeSession(exc=requests.ConnectionError(f"failed https://x?apikey={KEY}"))
    c = marketaux.MarketauxClient(KEY, sess, quota(tmp_path, "marketaux"))
    with pytest.raises(SourceError) as ei:
        c.news_all("TSM", "2026-11-23T21:00")
    assert ei.value.kind == "network" and KEY not in str(ei.value)


def test_bad_json(tmp_path):
    c = finnhub.FinnhubClient(KEY, FakeSession(FakeResp(None, 200, text="<html>")), quota(tmp_path, "finnhub"))
    with pytest.raises(SourceError) as ei:
        c.general_news()
    assert ei.value.kind == "bad_response"


# ---------------- Finnhub ----------------
def test_finnhub_request_and_normalize(tmp_path):
    rows = [{"category": "company", "datetime": 1732400000, "headline": "NVDA up", "id": 5,
             "related": "NVDA", "source": "Reuters", "summary": "s", "url": "http://u"}]
    sess = FakeSession(FakeResp(rows))
    c = finnhub.FinnhubClient(KEY, sess, quota(tmp_path, "finnhub"), timeout=7)
    got = c.company_news("NVDA", "2026-11-21", "2026-11-24")
    call = sess.calls[0]
    assert call["url"] == "https://finnhub.io/api/v1/company-news"
    assert call["params"] == {"symbol": "NVDA", "from": "2026-11-21", "to": "2026-11-24"}
    assert call["headers"] == {"X-Finnhub-Token": KEY} and call["timeout"] == 7
    assert KEY not in json.dumps(call["params"])
    item = finnhub.normalize_news(got, "NVDA")[0]
    assert item["feed"] == "finnhub:NVDA" and item["us_tickers"] == ["NVDA"]
    assert item["published_at"] == "2024-11-23T22:13:20Z" and item["title"] == "NVDA up"
    c.general_news(min_id=10)
    assert sess.calls[1]["url"].endswith("/news")
    assert sess.calls[1]["params"] == {"category": "general", "minId": 10}
    assert finnhub.normalize_news(rows)[0]["feed"] == "finnhub:general"


# ---------------- Fugle ----------------
def test_fugle_request(tmp_path):
    sess = FakeSession(FakeResp({"symbol": "2330"}))
    c = fugle.FugleClient(KEY, sess, quota(tmp_path, "fugle"))
    c.intraday_quote("2330")
    c.intraday_quote("2330", oddlot=True)
    assert sess.calls[0]["url"] == "https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/2330"
    assert sess.calls[0]["headers"] == {"X-API-KEY": KEY} and not sess.calls[0]["params"]
    assert sess.calls[1]["params"] == {"type": "oddlot"}


def _quote(**over):
    t = int(dt.datetime(2026, 11, 24, 10, 30, tzinfo=TAIPEI).timestamp() * 1_000_000)
    q = {"isTrial": False, "lastTrade": {"price": 1000.5, "time": t}, "tradingHalt": {"isHalted": False}}
    q.update(over)
    return q


def test_usable_last_trade():
    today = dt.date(2026, 11, 24)
    price, when = fugle.usable_last_trade(_quote(), today)
    assert price == 1000.5 and when.date() == today
    assert fugle.usable_last_trade(_quote(), dt.date(2026, 11, 25)) is None
    assert fugle.usable_last_trade(_quote(isTrial=True), today) is None
    assert fugle.usable_last_trade(_quote(tradingHalt={"isHalted": True}), today) is None
    assert fugle.usable_last_trade(_quote(lastTrade=None), today) is None
    assert fugle.usable_last_trade(_quote(lastTrade={"price": 0, "time": 1}), today) is None
    q = _quote(); del q["isTrial"]; del q["tradingHalt"]
    assert fugle.usable_last_trade(q, today)[0] == 1000.5
    assert fugle.usable_last_trade(None, today) is None


# ---------------- FinMind ----------------
def test_finmind_request_and_errors(tmp_path):
    sess = FakeSession(FakeResp({"msg": "success", "status": 200, "data": [{"date": "2026-11-01"}]}))
    c = finmind.FinMindClient(KEY, sess, quota(tmp_path, "finmind"))
    rows = c.data("TaiwanStockMonthRevenue", "2330", "2026-08-01", "2026-11-24")
    call = sess.calls[0]
    assert rows == [{"date": "2026-11-01"}]
    assert call["url"] == "https://api.finmindtrade.com/api/v4/data"
    assert call["params"] == {"dataset": "TaiwanStockMonthRevenue", "data_id": "2330",
                              "start_date": "2026-08-01", "end_date": "2026-11-24"}
    assert call["headers"] == {"Authorization": f"Bearer {KEY}"}
    c.user_info()
    assert sess.calls[1]["url"] == "https://api.web.finmindtrade.com/v2/user_info"
    body_err = finmind.FinMindClient(
        KEY, FakeSession(FakeResp({"status": 402, "msg": "Requests reach the upper limit"})), quota(tmp_path, "fm2"))
    with pytest.raises(SourceError) as ei:
        body_err.data("d", "1", "a", "b")
    assert ei.value.kind == "quota_exhausted"


# ---------------- FMP ----------------
def test_fmp_request(tmp_path):
    sess = FakeSession(FakeResp([{"symbol": "AAPL"}]))
    c = fmp.FmpClient(KEY, sess, quota(tmp_path, "fmp"))
    c.income_statement("AAPL", limit=1)
    c.balance_sheet_statement("AAPL")
    c.cash_flow_statement("AAPL", period="quarter")
    urls = [x["url"] for x in sess.calls]
    assert urls == [f"https://financialmodelingprep.com/stable/{p}" for p in
                    ("income-statement", "balance-sheet-statement", "cash-flow-statement")]
    assert sess.calls[0]["params"] == {"symbol": "AAPL", "period": "annual", "limit": 1, "apikey": KEY}
    assert sess.calls[2]["params"]["period"] == "quarter"


def test_fmp_error_body(tmp_path):
    c = fmp.FmpClient(KEY, FakeSession(FakeResp({"Error Message": f"Invalid {KEY}"})), quota(tmp_path, "fmp"))
    with pytest.raises(SourceError) as ei:
        c.income_statement("AAPL")
    assert ei.value.kind == "api_error" and KEY not in str(ei.value)


# ---------------- Massive ----------------
def _mpage(next_url=None):
    body = {"results": [{"id": "1", "publisher": {"name": "Benzinga"}, "title": "T", "published_utc": "2026-11-24T00:00:00Z",
                         "article_url": "http://a", "tickers": ["nvda"], "description": "d",
                         "insights": [{"ticker": "NVDA", "sentiment": "positive", "sentiment_reasoning": "r"}]}]}
    if next_url:
        body["next_url"] = next_url
    return body


def test_massive_request_normalize_and_paging(tmp_path):
    nxt = "https://api.massive.com/v2/reference/news?cursor=abc"
    sess = FakeSession(FakeResp(_mpage(nxt)), FakeResp(_mpage()))
    c = massive.MassiveClient(KEY, sess, quota(tmp_path, "massive"))
    pages = list(c.iter_news(max_pages=5, ticker="NVDA", published_gte="2026-11-23T00:00:00Z", limit=5))
    assert len(pages) == 2 and len(sess.calls) == 2
    assert sess.calls[0]["params"] == {"limit": 5, "ticker": "NVDA", "published_utc.gte": "2026-11-23T00:00:00Z"}
    assert sess.calls[0]["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert sess.calls[1]["url"] == nxt
    assert c.quota.used_today() == 2  # 翻頁也計次
    item = massive.normalize_news(pages[0])[0]
    assert item["feed"] == "massive:Benzinga" and item["us_tickers"] == ["NVDA"]
    assert item["insights"][0]["sentiment"] == "positive" and item["source"] == "Benzinga"


def test_massive_rejects_foreign_next_url(tmp_path):
    sess = FakeSession(FakeResp(_mpage()))
    c = massive.MassiveClient(KEY, sess, quota(tmp_path, "massive"))
    with pytest.raises(SourceError):
        c.news_next("https://evil.example.com/x")
    assert not sess.calls


# ---------------- Marketaux ----------------
def test_marketaux_request_and_normalize(tmp_path):
    body = {"meta": {}, "data": [{"uuid": "u", "title": "TSM", "description": "d", "url": "http://m",
                                  "published_at": "2026-11-24T00:00:00.000000Z", "source": "site.com",
                                  "entities": [{"symbol": "TSM", "sentiment_score": 0.5, "match_score": 9},
                                               {"symbol": "NVDA", "sentiment_score": 0.1}]}]}
    sess = FakeSession(FakeResp(body))
    c = marketaux.MarketauxClient(KEY, sess, quota(tmp_path, "marketaux"))
    out = c.news_all(["TSM", "NVDA"], "2026-11-23T21:00")
    p = sess.calls[0]["params"]
    assert p["symbols"] == "TSM,NVDA" and p["api_token"] == KEY and p["language"] == "en"
    assert p["published_after"] == "2026-11-23T21:00" and p["filter_entities"] == "true"
    item = marketaux.normalize_news(out)[0]
    assert item["feed"] == "marketaux:site.com" and item["us_tickers"] == ["TSM", "NVDA"]
    assert item["sentiment_score"] == pytest.approx(0.3)


# ---------------- Alpha Vantage ----------------
def test_alphavantage_request_and_normalize(tmp_path):
    body = {"feed": [{"title": "T", "url": "http://a", "time_published": "20261123T223518", "summary": "s",
                      "source": "CNBC", "overall_sentiment_score": 0.2, "overall_sentiment_label": "Neutral",
                      "ticker_sentiment": [{"ticker": "AAPL", "relevance_score": "0.5", "ticker_sentiment_score": "0.1"}]}]}
    sess = FakeSession(FakeResp(body))
    c = alphavantage.AlphaVantageClient(KEY, sess, quota(tmp_path, "alphavantage"))
    out = c.news_sentiment("financial_markets", "20261123T2100", limit=5)
    assert sess.calls[0]["params"] == {"function": "NEWS_SENTIMENT", "topics": "financial_markets",
                                       "time_from": "20261123T2100", "limit": 5, "apikey": KEY}
    item = alphavantage.normalize_news(out)[0]
    assert item["published_at"] == "2026-11-23T22:35:18Z" and item["feed"] == "alphavantage:CNBC"
    assert item["us_tickers"] == ["AAPL"] and item["ticker_sentiment"][0]["sentiment_score"] == 0.1
    assert item["sentiment_score"] == 0.2


def test_alphavantage_no_feed_is_error(tmp_path):
    msg = "Thank you ... rate limit is 25 requests per day."
    c = alphavantage.AlphaVantageClient(KEY, FakeSession(FakeResp({"Information": msg})), quota(tmp_path, "alphavantage"))
    with pytest.raises(SourceError) as ei:
        c.news_sentiment("technology", "20261123T2100")
    assert ei.value.kind == "quota_exhausted" and ei.value.status == 200
    c2 = alphavantage.AlphaVantageClient(KEY, FakeSession(FakeResp({})), quota(tmp_path, "alphavantage2"))
    with pytest.raises(SourceError) as ei2:
        c2.news_sentiment("technology", "20261123T2100")
    assert ei2.value.kind == "api_error"
    c3 = alphavantage.AlphaVantageClient(
        KEY, FakeSession(FakeResp({"Information": "This is a premium endpoint."})), quota(tmp_path, "alphavantage3"))
    with pytest.raises(SourceError) as ei3:
        c3.news_sentiment("technology", "20261123T2100")
    assert ei3.value.kind == "forbidden"


# ---------------- probe ----------------
def test_probe_skips_without_keys(tmp_path):
    sess = FakeSession(FakeResp([]))
    s = SimpleNamespace(finnhub_api_key="", fugle_api_key=" ", finmind_token="", fmp_api_key="",
                        massive_api_key="", marketaux_api_token="", alphavantage_api_key="")
    res = run_probe(s, session=sess, quota_dir=tmp_path)
    assert {r["status"] for r in res} == {"skipped"} and len(res) == 7
    assert not sess.calls and not list(tmp_path.iterdir())


def test_probe_reports_keys_only_and_goes_through_quota(tmp_path):
    sess = FakeSession(FakeResp([{"headline": "secret-headline-body", "url": "u"}]))
    s = SimpleNamespace(finnhub_api_key=KEY)
    res = run_probe(s, sources=["finnhub", "fmp"], session=sess, quota_dir=tmp_path, today=dt.date(2026, 11, 24))
    fh = res[0]
    assert fh["status"] == "ok" and fh["count"] == 1 and fh["first_keys"] == ["headline", "url"]
    assert res[1]["status"] == "skipped"
    dumped = json.dumps(res)
    assert KEY not in dumped and "secret-headline-body" not in dumped
    assert (tmp_path / "finnhub.json").exists()
    assert sess.calls[0]["params"]["from"] == "2026-11-21"


def test_probe_error_classified(tmp_path):
    sess = FakeSession(FakeResp(None, 429, text="slow down"))
    res = run_probe(SimpleNamespace(finnhub_api_key=KEY), sources=["finnhub"], session=sess, quota_dir=tmp_path)
    assert res[0]["status"] == "error" and res[0]["error"] == "rate_limited" and res[0]["http_status"] == 429


def test_quota_remaining(tmp_path):
    clock = [dt.datetime(2026, 11, 24, 21, 0, tzinfo=TAIPEI)]
    q = QuotaTracker("x", per_hour=3, daily_limit=10, directory=tmp_path, now=lambda: clock[0])
    assert q.remaining() == 3
    q.consume()
    q.consume()
    assert q.remaining() == 1
    clock[0] += dt.timedelta(hours=1, seconds=1)               # 舊的請求出了小時視窗
    assert q.remaining() == 3
    assert QuotaTracker("y", directory=tmp_path).remaining() is None

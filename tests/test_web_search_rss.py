"""RSS 來源、DDG 熔斷與 source_status 的測試；全部使用假 session。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot import web_search as ws
from bot.web_search import (
    PageContent,
    SearchResult,
    SearchUnavailable,
    WebMaterial,
    research_ticker,
    search_feeds,
)

RSS_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<item><title>台積電 2330 法說會</title><link>https://a.example/1</link>
<description>內容</description><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>無關新聞</title><link>https://a.example/2</link><description>其他</description></item>
<item><title>摘要提到</title><link>https://a.example/3</link><description>代號 2330 上漲</description></item>
</channel></rss>""".encode("utf-8")

ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>台積電擴產</title><link href="https://b.example/1"/>
<summary>x</summary><updated>2024-01-02T00:00:00Z</updated></entry>
<entry><title>別的公司</title><link href="https://b.example/2"/><summary>y</summary></entry>
</feed>""".encode("utf-8")


class FakeResp:
    def __init__(self, content=b"", status=200):
        self.content = content
        self.status_code = status
        self.text = content.decode("utf-8", "ignore")


class FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        val = self.mapping.get(url)
        if isinstance(val, Exception):
            raise val
        return val if val is not None else FakeResp(b"", 404)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ws.time, "sleep", lambda *_: None)


def test_rss_and_atom_filtering():
    feeds = (("rss1", "http://r/1"), ("atom1", "http://r/2"))
    sess = FakeSession({"http://r/1": FakeResp(RSS_XML), "http://r/2": FakeResp(ATOM_XML)})
    results, status = search_feeds("2330", "台積電", feeds=feeds, session=sess)
    urls = [r.url for r in results]
    assert urls == ["https://a.example/1", "https://a.example/3", "https://b.example/1"]
    assert all(r.source == "rss" for r in results)
    assert results[0].site == "rss1" and results[0].published_at
    assert status == {"rss1": "ok", "atom1": "ok"}


def test_filter_by_ticker_only_when_name_empty():
    sess = FakeSession({"http://r/1": FakeResp(ATOM_XML)})
    results, status = search_feeds("2330", "", feeds=(("a", "http://r/1"),), session=sess)
    assert results == []
    assert status["a"] == "ok (0 matched)"


def test_single_feed_failure_isolated():
    feeds = (("bad", "http://r/bad"), ("boom", "http://r/boom"), ("garbage", "http://r/g"), ("good", "http://r/ok"))
    sess = FakeSession({
        "http://r/bad": FakeResp(b"", 503),
        "http://r/boom": RuntimeError("timeout"),
        "http://r/g": FakeResp(b"<not xml"),
        "http://r/ok": FakeResp(RSS_XML),
    })
    results, status = search_feeds("2330", "", feeds=feeds, session=sess)
    assert status["bad"] == "unavailable: HTTP 503"
    assert status["boom"].startswith("unavailable:") and "timeout" in status["boom"]
    assert status["garbage"].startswith("unavailable:")
    assert status["good"] == "ok"
    assert len(results) == 2


def _patch_research(monkeypatch, ddg_side_effect, rss_results=None, rss_status=None):
    calls = {"ddg": 0, "fetch": []}

    def fake_ddg(q, **kw):
        calls["ddg"] += 1
        assert kw.get("strict") is True
        return ddg_side_effect()

    def fake_fetch(url, **kw):
        calls["fetch"].append(url)
        return PageContent(url=url, title="t", text="body " * 10)

    monkeypatch.setattr(ws, "search_news", lambda q, **kw: [])
    monkeypatch.setattr(ws, "search_web", fake_ddg)
    monkeypatch.setattr(
        ws, "search_feeds",
        lambda *a, **kw: (rss_results or [], rss_status or {"f1": "ok"}),
    )
    monkeypatch.setattr(ws, "fetch_page_text", fake_fetch)
    return calls


def test_ddg_circuit_breaker_and_status(monkeypatch, tmp_path: Path):
    def raise_unavail():
        raise SearchUnavailable("DuckDuckGo HTTP 202")

    calls = _patch_research(monkeypatch, raise_unavail)
    m = research_ticker("2330", name="台積電", root=tmp_path, force_refresh=True)
    assert calls["ddg"] == 1
    assert m.source_status["ddg"] == "unavailable: DuckDuckGo HTTP 202"
    assert m.source_status["google_news"] == "ok (0 results)"
    assert m.source_status["rss:f1"] == "ok"
    text = m.compact_text()
    assert "【來源狀態】" in text and "DuckDuckGo 網頁搜尋：不可用" in text


def test_ddg_ok_status(monkeypatch, tmp_path: Path):
    _patch_research(monkeypatch, lambda: [])
    m = research_ticker("2330", name="台積電", root=tmp_path, force_refresh=True)
    assert m.source_status["ddg"] == "ok"
    assert "【來源狀態】" not in m.compact_text()


def test_rss_results_fetched_for_pages(monkeypatch, tmp_path: Path):
    rss = [SearchResult(title="t", url="https://a.example/1", source="rss", site="f1")]
    calls = _patch_research(monkeypatch, lambda: [], rss_results=rss)
    m = research_ticker("2330", name="台積電", root=tmp_path, force_refresh=True)
    assert calls["fetch"] == ["https://a.example/1"]
    assert len(m.pages) == 1


def test_cache_roundtrip_and_legacy_cache(monkeypatch, tmp_path: Path):
    _patch_research(monkeypatch, lambda: [])
    research_ticker("2330", name="台積電", root=tmp_path, force_refresh=True)
    cached = research_ticker("2330", name="台積電", root=tmp_path)
    assert cached.source_status["ddg"] == "ok"

    p = ws._cache_path("2330", tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data.pop("source_status")
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    legacy = research_ticker("2330", name="台積電", root=tmp_path)
    assert legacy.source_status == {}
    assert isinstance(legacy, WebMaterial)

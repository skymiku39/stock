"""RSS 來源、DDG 熔斷與 source_status 的測試；全部使用假 session。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot import web_search as ws
from bot.web_search import (
    Feed,
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
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), 4096):
            yield self.content[i:i + 4096]

    def close(self):
        self.closed = True


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
    ws._RSS_MEM.clear()
    yield
    ws._RSS_MEM.clear()


def F(name, url, region="tw", lang="zh"):
    return Feed(name, url, region, lang)


def test_rss_and_atom_filtering():
    feeds = (F("rss1", "http://r/1"), F("atom1", "http://r/2"))
    sess = FakeSession({"http://r/1": FakeResp(RSS_XML), "http://r/2": FakeResp(ATOM_XML)})
    results, status = search_feeds("2330", "台積電", feeds=feeds, session=sess)
    urls = [r.url for r in results]
    assert urls == ["https://a.example/1", "https://a.example/3", "https://b.example/1"]
    assert all(r.source == "rss" for r in results)
    assert results[0].site == "rss1" and results[0].published_at
    assert status == {"rss1": "ok", "atom1": "ok"}


def test_filter_by_ticker_only_when_name_empty():
    sess = FakeSession({"http://r/1": FakeResp(ATOM_XML)})
    results, status = search_feeds("2330", "", feeds=(F("a", "http://r/1"),), session=sess)
    assert results == []
    assert status["a"] == "ok (0 matched)"


def test_single_feed_failure_isolated():
    feeds = (F("bad", "http://r/bad"), F("boom", "http://r/boom"), F("garbage", "http://r/g"), F("good", "http://r/ok"))
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


def _patch_research(monkeypatch, ddg_side_effect, rss_results=None, rss_status=None, world_results=None):
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
    monkeypatch.setattr(
        ws, "search_world_news", lambda **kw: (world_results or [], {"f": "ok"}),
    )
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


# ----------------------------------------------------------------------
# 快取 / deadline / 英文比對 / world
# ----------------------------------------------------------------------

EN_XML = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>TSMC raises capex</title><link>https://e.example/1</link><description>chips</description></item>
<item><title>TSMCX unrelated</title><link>https://e.example/2</link><description>x</description></item>
<item><title>Ase of use</title><link>https://e.example/3</link><description>the base case</description></item>
<item><title>ASE reports earnings</title><link>https://e.example/4</link><description>y</description></item>
</channel></rss>"""


def test_cache_ttl_and_raw_saved_once(monkeypatch, tmp_path: Path):
    feed = F("c1", "http://r/c1")
    sess = FakeSession({feed.url: FakeResp(RSS_XML)})
    saved = []

    class FakeRegistry:
        def __init__(self, root):
            pass

        def capture_document(self, source, content, **kw):
            saved.append(source)
            return type("D", (), {"document_id": "doc-1"})()

    monkeypatch.setattr(ws, "SourceRegistry", FakeRegistry)
    now = [1000.0]
    monkeypatch.setattr(ws.time, "time", lambda: now[0])
    c1, raw1, cached1 = ws._fetch_feed(feed, session=sess, root=tmp_path)
    assert (raw1, cached1) == ("doc-1", False)
    assert (tmp_path / "data/cache/rss/c1.xml").exists()
    meta = json.loads((tmp_path / "data/cache/rss/c1.json").read_text(encoding="utf-8"))
    assert meta["fetched_at"] == 1000.0 and meta["raw_document_id"] == "doc-1"
    now[0] += 100
    c2, raw2, cached2 = ws._fetch_feed(feed, session=sess, root=tmp_path)
    assert (c2, raw2, cached2) == (c1, "doc-1", True)
    assert len(sess.calls) == 1 and saved == ["rss_feed"]
    now[0] += 5000
    _, _, cached3 = ws._fetch_feed(feed, session=sess, root=tmp_path)
    assert cached3 is False and len(sess.calls) == 2 and len(saved) == 2
    ws._fetch_feed(feed, session=sess, root=tmp_path, ttl_seconds=0)
    assert len(sess.calls) == 3


def test_memory_cache_without_root_and_no_sleep_on_cache(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ws.time, "sleep", lambda s: sleeps.append(s))
    feeds = (F("m1", "http://r/m1"), F("m2", "http://r/m2"))
    sess = FakeSession({"http://r/m1": FakeResp(RSS_XML), "http://r/m2": FakeResp(RSS_XML)})
    search_feeds("2330", "", feeds=feeds, session=sess)
    assert len(sess.calls) == 2 and sleeps == [0.3, 0.3]
    sleeps.clear()
    search_feeds("2330", "", feeds=feeds, session=sess)
    assert len(sess.calls) == 2 and sleeps == []


def test_stale_cache_used_when_fetch_fails(monkeypatch, tmp_path: Path):
    feed = F("s1", "http://r/s1")
    now = [1000.0]
    monkeypatch.setattr(ws.time, "time", lambda: now[0])
    search_feeds("2330", "", feeds=(feed,), session=FakeSession({feed.url: FakeResp(RSS_XML)}),
                 root=tmp_path)
    now[0] += 10_000
    bad = FakeSession({feed.url: FakeResp(b"", 503)})
    results, status = search_feeds("2330", "", feeds=(feed,), session=bad, root=tmp_path)
    assert len(bad.calls) == 1
    assert status["s1"] == "stale cache (HTTP 503)"
    assert len(results) == 2
    other = F("s2", "http://r/s2")
    _, status2 = search_feeds("2330", "", feeds=(other,), session=bad, root=tmp_path)
    assert status2["s2"] == "unavailable: HTTP 404"


def test_deadline_exceeded_fails(monkeypatch):
    feed = F("d1", "http://r/d1")
    ticks = iter([0.0, 1.0, 50.0, 60.0, 70.0])
    monkeypatch.setattr(ws.time, "monotonic", lambda: next(ticks))
    with pytest.raises(Exception, match="deadline"):
        ws._fetch_feed(feed, session=FakeSession({feed.url: FakeResp(RSS_XML * 3)}), deadline=20)
    assert ("d1", feed.url) not in ws._RSS_MEM


def test_oversized_response_fails():
    feed = F("big", "http://r/big")
    big = b"x" * (ws.RSS_MAX_BYTES + 10)
    with pytest.raises(Exception, match="too large"):
        ws._fetch_feed(feed, session=FakeSession({feed.url: FakeResp(big)}))
    assert ("big", feed.url) not in ws._RSS_MEM


def test_request_uses_stream_and_hard_timeout():
    seen = {}

    class S:
        def get(self, url, **kw):
            seen.update(kw)
            return FakeResp(RSS_XML)

    ws._fetch_feed(F("t1", "http://r/t1"), session=S())
    assert seen["stream"] is True and seen["timeout"] == (8, 8)


def test_english_word_boundary_matching():
    feed = F("en1", "http://r/en", "us", "en")
    sess = FakeSession({feed.url: FakeResp(EN_XML)})
    results, _ = search_feeds("2330", "台積電", english_name="TSMC", feeds=(feed,), session=sess)
    assert [r.url for r in results] == ["https://e.example/1"]  # TSMCX not matched
    results, _ = search_feeds("6999", "", english_name="ASE", feeds=(feed,), session=sess)
    assert [r.url for r in results] == ["https://e.example/4"]  # not "base", not "Ase"
    results, _ = search_feeds("1", "", english_name="Raises", feeds=(feed,), session=sess)
    assert [r.url for r in results] == ["https://e.example/1"]  # long names are case-insensitive


def test_en_feed_skipped_without_english_name():
    feeds = (F("en1", "http://r/en", "us", "en"), F("zh1", "http://r/zh"))
    sess = FakeSession({"http://r/en": FakeResp(EN_XML), "http://r/zh": FakeResp(RSS_XML)})
    _, status = search_feeds("2330", "台積電", feeds=feeds, session=sess)
    assert status["en1"] == "skipped (no english name)"
    assert status["zh1"] == "ok"
    assert sess.calls == ["http://r/zh"]


WORLD_XML = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Trump says tariff on chips</title><link>https://w.example/1</link>
<description>trade war</description><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>Trump meets Xi Jinping</title><link>https://w.example/2</link>
<description>talks</description><pubDate>Tue, 02 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>Trump golf</title><link>https://w.example/3</link><description>weekend</description>
<pubDate>Wed, 03 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>tariff and sanctions news</title><link>https://w.example/4</link><description>x</description>
<pubDate>Thu, 04 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>Only a tariff</title><link>https://w.example/5</link><description>x</description></item>
<item><title>Trumpet tariff</title><link>https://w.example/6</link><description>x</description></item>
<item><title>習近平談關稅</title><link>https://w.example/7</link><description>x</description>
<pubDate>Fri, 05 Jan 2024 00:00:00 GMT</pubDate></item>
</channel></rss>""".encode("utf-8")

WATCH = {
    "leaders": {"US": ["Donald Trump", "Trump", "川普"], "China": ["Xi Jinping", "習近平"]},
    "topics": ["tariff", "tariffs", "trade war", "sanctions", "關稅"],
}


def test_world_threshold_and_ordering():
    feeds = (F("w1", "http://r/w1", "us", "en"), F("w2", "http://r/w2", "uk", "en"))
    sess = FakeSession({"http://r/w1": FakeResp(WORLD_XML), "http://r/w2": FakeResp(WORLD_XML)})
    results, status = ws.search_world_news(session=sess, feeds=feeds, watch=WATCH)
    # score 4: item2 (Jan 2) before item1 (Jan 1); score 3: item7; score 2: item4 then item3.
    # item5 (one topic) and item6 ("Trumpet" is not "Trump") are below threshold; URLs dedupe.
    assert [r.url for r in results] == [f"https://w.example/{i}" for i in (2, 1, 7, 4, 3)]
    assert all(r.source == "rss_world" and r.site in ("w1", "w2") for r in results)
    by_url = {r.url: r for r in results}
    assert by_url["https://w.example/1"].snippet.startswith("[Trump, tariff, trade war] ")
    assert set(status) == {"w1", "w2"}
    limited, _ = ws.search_world_news(session=sess, feeds=feeds, watch=WATCH, max_results=2)
    assert len(limited) == 2


def test_load_world_watch(tmp_path: Path, monkeypatch):
    # A data root without config/ falls back to the repo's config.
    assert ws.load_world_watch(tmp_path)["leaders"]
    monkeypatch.setattr(ws, "WORLD_WATCH_REL", Path("config/__missing_world_watch__.json"))
    assert ws.load_world_watch(tmp_path) == {"leaders": {}, "topics": []}
    monkeypatch.undo()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "world_watch.json").write_text(
        json.dumps({"leaders": {"US": ["Trump"]}, "topics": ["tariff"]}), encoding="utf-8",
    )
    assert ws.load_world_watch(tmp_path)["leaders"] == {"US": ["Trump"]}
    repo_cfg = ws.load_world_watch(None)
    assert repo_cfg["leaders"] and repo_cfg["topics"]


def test_world_results_in_compact_text_cache_and_status(monkeypatch, tmp_path: Path):
    world = [SearchResult(title="W title", url="https://w/1", snippet="[Trump, tariff] " + "x" * 300,
                          source="rss_world", site="bbc_world")]
    _patch_research(monkeypatch, lambda: [], world_results=world)
    m = research_ticker("2330", name="台積電", root=tmp_path, force_refresh=True)
    assert m.source_status["world"] == "ok (1)"
    assert not any(k.startswith("rss:bbc") for k in m.source_status)
    assert m.search_results == [] and m.pages == []
    text = m.compact_text()
    assert "【國際與地緣政治】" in text
    line = [ln for ln in text.splitlines() if ln.startswith("- (bbc_world) W title")][0]
    assert len(line.split(" — ", 1)[1]) == 160
    cached = research_ticker("2330", name="台積電", root=tmp_path)
    assert [r.url for r in cached.world_results] == ["https://w/1"]


def test_legacy_cache_without_world_results(monkeypatch, tmp_path: Path):
    _patch_research(monkeypatch, lambda: [])
    research_ticker("2330", name="台積電", root=tmp_path, force_refresh=True)
    p = ws._cache_path("2330", tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data.pop("world_results")
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    legacy = research_ticker("2330", name="台積電", root=tmp_path)
    assert legacy.world_results == []


def test_world_status_all_failed(monkeypatch, tmp_path: Path):
    _patch_research(monkeypatch, lambda: [])
    monkeypatch.setattr(
        ws, "search_world_news",
        lambda **kw: ([], {"a": "unavailable: HTTP 503", "b": "unavailable: HTTP 503"}),
    )
    m = research_ticker("2330", name="台積電", root=tmp_path, force_refresh=True)
    assert m.source_status["world"].startswith("unavailable:")
    assert "國際與地緣政治：不可用" in m.compact_text()


def test_research_passes_english_name(monkeypatch, tmp_path: Path):
    _patch_research(monkeypatch, lambda: [])
    seen = {}
    monkeypatch.setattr(
        ws, "search_feeds",
        lambda *a, **kw: (seen.update(kw) or [], {"f1": "ok"}),
    )
    import bot.company_info as ci

    monkeypatch.setattr(ci, "english_name_for", lambda sym, root=None: "TSMC")
    research_ticker("2330", name="台積電", root=tmp_path, force_refresh=True)
    assert seen["english_name"] == "TSMC"
    research_ticker("2330", name="台積電", root=tmp_path, force_refresh=True, english_name="ASE")
    assert seen["english_name"] == "ASE"


def test_rss_feed_catalog():
    names = [f.name for f in ws.RSS_FEEDS]
    assert len(names) == len(set(names)) == 70
    assert all(f.region in {"tw", "us", "uk", "eu", "asia"} and f.lang in {"zh", "en"}
               for f in ws.RSS_FEEDS)
    assert all(f.lang == "zh" for f in ws.RSS_FEEDS if f.region == "tw")
    assert all(f.lang == "en" for f in ws.RSS_FEEDS if f.region != "tw")


def test_validate_check_rss_feeds_parallel_and_by_region(monkeypatch):
    from bot import validate_sources as vs

    feeds = (F("a", "http://r/a"), F("b", "http://r/b", "us", "en"), F("c", "http://r/c", "us", "en"))
    sess = FakeSession({"http://r/a": FakeResp(RSS_XML), "http://r/b": FakeResp(EN_XML)})
    monkeypatch.setattr(ws, "RSS_FEEDS", feeds)
    monkeypatch.setattr(ws, "_new_session", lambda: sess)
    res = vs.check_rss_feeds(None)
    assert res.ok and res.details["usable"] == 2
    assert set(res.details["unavailable"]) == {"c"}
    assert res.details["by_region"] == {"tw": {"usable": 1, "total": 1}, "us": {"usable": 1, "total": 2}}
    assert res.rows == 3 + 4


def test_result_cap_is_per_language():
    zh_items = "".join(
        f"<item><title>台積電 新聞{i}</title><link>https://z.example/{i}</link></item>" for i in range(5)
    )
    en_items = "".join(
        f"<item><title>TSMC story {i}</title><link>https://e.example/{i}</link></item>" for i in range(5)
    )
    zh = F("zh", "http://r/zh")
    en = F("en", "http://r/en", "us", "en")
    sess = FakeSession({
        zh.url: FakeResp(f"<rss><channel>{zh_items}</channel></rss>".encode("utf-8")),
        en.url: FakeResp(f"<rss><channel>{en_items}</channel></rss>".encode("utf-8")),
    })
    results, _ = search_feeds("2330", "台積電", english_name="TSMC", feeds=(zh, en), session=sess, max_results=3)
    assert [r.site for r in results] == ["zh"] * 3 + ["en"] * 3

import datetime as dt
import json
from types import SimpleNamespace

from bot import rss_archive


def _feed(name):
    return SimpleNamespace(name=name, url="https://example.test/" + name, region="tw", lang="zh", stance="")


def _xml(*entries):
    body = "".join(f"<item><title>{t}</title><link>{u}</link><pubDate>{p}</pubDate></item>" for t, u, p in entries)
    return f"<rss><channel>{body}</channel></rss>".encode()


def test_archive_accumulates_dedupes_skips_old_and_resumes_after_budget(tmp_path, monkeypatch):
    clock = [dt.datetime(2026, 11, 17, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))]
    monkeypatch.setattr(rss_archive, "now_tw", lambda: clock[0])
    feeds = [_feed("a"), _feed("b"), _feed("c")]
    today, old = "Tue, 17 Nov 2026 01:00:00 GMT", "Sun, 01 Nov 2026 01:00:00 GMT"
    pages = {"a": _xml(("A1", "https://x.test/1", today), ("舊聞", "https://x.test/old", old)),
             "b": None, "c": _xml(("C1", "https://x.test/3", today), ("重複", "https://x.test/1", today))}
    calls = []
    def loader(feed, **kw):
        calls.append(feed.name)
        content = pages[feed.name]
        return content, "doc-" + feed.name, "ok" if content else "unavailable: HTTP 503"
    # 預算 0：一個來源都不抓，但仍寫出檔案並保留游標。
    first = rss_archive.collect_rss_archive(tmp_path, feeds=feeds, budget=0, loader=loader, session=object())
    assert first["feeds_visited"] == 0 and first["status"] == "failed" and not calls
    done = rss_archive.collect_rss_archive(tmp_path, feeds=feeds, budget=60, loader=loader, session=object())
    assert calls == ["a", "b", "c"] and done["status"] == "ok"
    assert done["total_items"] == 2 and done["new_items"] == 2 and done["today_items"] == 2
    assert done["feeds_ok"] == 2 and done["feeds_failed"] == ["b"]
    saved = json.loads(rss_archive.archive_path(tmp_path, "2026-11-17").read_text(encoding="utf-8"))
    assert [i["title"] for i in saved["items"]] == ["A1", "C1"]
    assert saved["items"][0]["feed"] == "a" and saved["items"][0]["source_document_id"] == "doc-a"
    # 後續新文章併入既有清單；來源恢復後狀態更新。
    pages["b"] = _xml(("B1", "https://x.test/2", today))
    again = rss_archive.collect_rss_archive(tmp_path, feeds=feeds, budget=60, loader=loader, session=object())
    assert again["new_items"] == 1 and again["total_items"] == 3 and again["feeds_failed"] == []
    # 換日另開新檔。
    clock[0] += dt.timedelta(days=1)
    assert rss_archive.collect_rss_archive(tmp_path, feeds=feeds, budget=0, loader=loader,
                                           session=object())["date"] == "2026-11-18"


def test_unparseable_feed_is_reported_without_losing_others(tmp_path, monkeypatch):
    monkeypatch.setattr(rss_archive, "now_tw", lambda: dt.datetime(2026, 11, 17, 10, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    pages = {"bad": b"<html>not a feed", "ok": _xml(("T", "https://x.test/t", ""))}
    out = rss_archive.collect_rss_archive(tmp_path, feeds=[_feed("bad"), _feed("ok")], budget=60, session=object(),
                                          loader=lambda feed, **kw: (pages[feed.name], "", "ok"))
    assert out["total_items"] == 1 and out["feeds_failed"] == ["bad"]


def test_items_are_tagged_with_tracked_tickers_without_matching_bare_numbers(tmp_path, monkeypatch):
    from bot import company_info
    from bot.stock_db import StockDB, StockInfo
    monkeypatch.setattr(rss_archive, "now_tw", lambda: dt.datetime(2026, 11, 17, 10, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    monkeypatch.setattr(company_info, "english_name_for", lambda symbol, root=None: {"2330": "TSMC"}.get(symbol, ""))
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_stock_info(StockInfo("2330", "台灣積體電路製造", short_name="台積電"))
        db.upsert_stock_info(StockInfo("2303", "聯華電子", short_name="聯電"))
    finally:
        db.close()
    zh = SimpleNamespace(name="zh", url="https://example.test/zh", region="tw", lang="zh", stance="")
    en = SimpleNamespace(name="en", url="https://example.test/en", region="us", lang="en", stance="")
    pages = {"zh": _xml(("台積電法說優於預期", "https://x.test/a", ""), ("外資買超 2330 億元", "https://x.test/b", ""),
                        ("晶圓雙雄 (2303) 同步走高", "https://x.test/c", "")),
             "en": _xml(("TSMC raises capex", "https://x.test/d", ""), ("Chipmakers slide", "https://x.test/e", ""))}
    out = rss_archive.collect_rss_archive(tmp_path, feeds=[zh, en], budget=60, session=object(), tracked=["2330", "2303"],
                                          loader=lambda feed, **kw: (pages[feed.name], "", "ok"))
    saved = json.loads(rss_archive.archive_path(tmp_path, "2026-11-17").read_text(encoding="utf-8"))
    tags = {i["url"][-1]: i["related_tickers"] for i in saved["items"]}
    assert tags == {"a": ["2330"], "b": [], "c": ["2303"], "d": ["2330"], "e": []}
    assert out["tagged_items"] == 3 and out["top_tickers"] == [["2330", 2], ["2303", 1]]


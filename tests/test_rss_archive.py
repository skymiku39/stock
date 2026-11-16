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

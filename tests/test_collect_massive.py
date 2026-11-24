"""Massive 新聞收集器（離線；假 Session）。"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from bot.free_sources import collect_massive as cm
from bot.free_sources.quota import TAIPEI, QuotaTracker

KEY = "SECRET-KEY-123"
NOW = dt.datetime(2026, 11, 24, 8, 0, tzinfo=TAIPEI)  # 週二台灣早上 = 美東週一晚上
SETTINGS = SimpleNamespace(massive_api_key=KEY)
NEXT = "https://api.massive.com/v2/reference/news?cursor=abc"


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
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def art(n, publisher="Benzinga", url=None):
    return {
        "id": str(n), "publisher": {"name": publisher}, "title": f"title {n}",
        "published_utc": "2026-11-23T22:00:00Z", "article_url": url or f"https://x/{n}",
        "tickers": ["nvda"], "description": "d",
        "insights": [{"ticker": "NVDA", "sentiment": "positive", "sentiment_reasoning": "because"}],
    }


def page(*arts, nxt=None):
    body = {"results": list(arts)}
    if nxt:
        body["next_url"] = nxt
    return FakeResp(body)


def run(tmp_path, sess, **kw):
    q = QuotaTracker("massive", daily_limit=200, per_minute=4, directory=tmp_path / "q")
    kw.setdefault("quota", q)
    return cm.collect_massive(tmp_path, SETTINGS, session=sess, now=NOW, **kw)


def out_file(tmp_path):
    return tmp_path / "data" / "news" / "api_massive_2026-11-24.json"


def test_no_key_is_skipped(tmp_path):
    sess = FakeSession(page(art(1)))
    res = cm.collect_massive(tmp_path, SimpleNamespace(massive_api_key=" "), session=sess, now=NOW)
    assert res["status"] == "skipped" and not sess.calls and not (tmp_path / "data").exists()


def test_request_has_time_range_and_no_ticker(tmp_path):
    sess = FakeSession(page(art(1)))
    run(tmp_path, sess)
    c = sess.calls[0]
    assert "ticker" not in c["params"]
    assert c["params"]["published_utc.gte"] == "2026-11-23T21:00:00Z"  # 週一 16:00 EST
    assert c["params"]["limit"] == cm.PAGE_LIMIT
    assert c["headers"] == {"Authorization": f"Bearer {KEY}"}


@pytest.mark.parametrize("now_utc,expected", [
    # 週二盤前 -> 週一收盤（冬令 EST，UTC-5）
    ("2026-11-24T12:00:00+00:00", "2026-11-23T21:00:00Z"),
    # 週二收盤後 -> 週二收盤
    ("2026-11-24T22:00:00+00:00", "2026-11-24T21:00:00Z"),
    # 週一盤前 -> 上週五收盤
    ("2026-11-23T12:00:00+00:00", "2026-11-20T21:00:00Z"),
    # 週日 -> 週五收盤
    ("2026-11-22T12:00:00+00:00", "2026-11-20T21:00:00Z"),
    # 夏令 EDT（UTC-4）：週四盤前 -> 週三收盤
    ("2026-07-09T12:00:00+00:00", "2026-07-08T20:00:00Z"),
])
def test_window_start_cases(now_utc, expected):
    assert cm.iso_utc(cm.window_start(dt.datetime.fromisoformat(now_utc))) == expected


def test_paging_respects_max_pages(tmp_path):
    sess = FakeSession(page(art(1), nxt=NEXT), page(art(2), nxt=NEXT), page(art(3), nxt=NEXT), page(art(4)))
    res = run(tmp_path, sess, max_pages=3)
    assert len(sess.calls) == 3 and res["pages"] == 3 and res["new_items"] == 3


def test_paging_stops_without_next_url(tmp_path):
    sess = FakeSession(page(art(1)))
    res = run(tmp_path, sess)
    assert len(sess.calls) == 1 and res["pages"] == 1


def test_items_keep_feed_and_insights(tmp_path):
    run(tmp_path, FakeSession(page(art(1, publisher="Reuters"))))
    item = json.loads(out_file(tmp_path).read_text(encoding="utf-8"))[0]
    assert item["feed"] == "massive:Reuters" and item["us_tickers"] == ["NVDA"]
    assert item["insights"][0] == {"ticker": "NVDA", "sentiment": "positive", "reasoning": "because"}


def test_writes_list_to_taiwan_dated_file(tmp_path):
    res = run(tmp_path, FakeSession(page(art(1), art(2))))
    data = json.loads(out_file(tmp_path).read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 2 and res["status"] == "ok"


def test_merge_dedups_by_url(tmp_path):
    run(tmp_path, FakeSession(page(art(1), art(2))))
    res = run(tmp_path, FakeSession(page(art(2), art(3))))
    data = json.loads(out_file(tmp_path).read_text(encoding="utf-8"))
    assert [i["url"] for i in data] == ["https://x/1", "https://x/2", "https://x/3"]
    assert res["new_items"] == 1 and res["total_items"] == 3
    run(tmp_path, FakeSession(page(art(1), art(2), art(3))))
    assert len(json.loads(out_file(tmp_path).read_text(encoding="utf-8"))) == 3


def test_atomic_write_leaves_no_temp(tmp_path):
    run(tmp_path, FakeSession(page(art(1))))
    assert [p.name for p in out_file(tmp_path).parent.iterdir()] == [out_file(tmp_path).name]


def test_auth_error_reported_not_raised(tmp_path):
    res = run(tmp_path, FakeSession(FakeResp(None, 401, text=f"Unknown API Key {KEY}")))
    assert res["status"] == "error" and res["error"] == "auth" and res["http_status"] == 401
    assert not out_file(tmp_path).exists()
    assert KEY not in json.dumps(res)


def test_quota_exceeded_stops(tmp_path):
    q = QuotaTracker("massive", daily_limit=1, directory=tmp_path / "q")
    sess = FakeSession(page(art(1), nxt=NEXT), page(art(2)))
    res = run(tmp_path, sess, quota=q)
    assert res["status"] == "quota" and len(sess.calls) == 1 and res["new_items"] == 1
    assert "quota_exceeded" in res["error"]


def test_429_stops_and_keeps_collected_pages(tmp_path):
    sess = FakeSession(page(art(1), nxt=NEXT), FakeResp(None, 429, text="slow"))
    res = run(tmp_path, sess)
    assert res["status"] == "quota" and res["error"] == "rate_limited" and res["pages"] == 1
    assert len(json.loads(out_file(tmp_path).read_text(encoding="utf-8"))) == 1


def test_dry_run_no_request_no_write(tmp_path):
    sess = FakeSession(page(art(1)))
    res = run(tmp_path, sess, dry_run=True)
    assert res["status"] == "dry_run" and res["params"]["published_utc.gte"] == "2026-11-23T21:00:00Z"
    assert not sess.calls and not (tmp_path / "data").exists()


def test_summary_never_contains_key(tmp_path):
    res = run(tmp_path, FakeSession(page(art(1))))
    assert KEY not in json.dumps(res)


@pytest.mark.parametrize("status,code", [("ok", 0), ("skipped", 0), ("dry_run", 0), ("error", 2), ("quota", 2)])
def test_main_exit_codes(monkeypatch, capsys, tmp_path, status, code):
    seen = {}

    def fake(root, **kw):
        seen.update(kw)
        return {"status": status}

    monkeypatch.setattr(cm, "collect_massive", fake)
    assert cm.main(["--root", str(tmp_path), "--dry-run", "--max-pages", "2"]) == code
    assert seen == {"max_pages": 2, "dry_run": True}
    assert json.loads(capsys.readouterr().out)["status"] == status

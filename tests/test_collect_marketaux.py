"""Marketaux 隔夜情緒收集（離線；假 client / 假 Session，不連網）。"""
from __future__ import annotations

import datetime as dt
import json

from bot.free_sources import collect_marketaux as cm
from bot.free_sources.base import SourceError
from bot.free_sources.marketaux import MarketauxClient
from bot.free_sources.quota import TAIPEI, QuotaExceeded, QuotaTracker

NOW = dt.datetime(2026, 11, 24, 7, 30, tzinfo=TAIPEI)
DAY = "2026-11-24"
KEY = "MX-SECRET-999"


def art(uuid, sym_scores, url=None):
    return {
        "uuid": uuid,
        "title": f"title {uuid}",
        "description": "d",
        "url": url or f"https://x.test/{uuid}",
        "published_at": "2026-11-23T22:00:00.000000Z",
        "source": "x.test",
        "entities": [{"symbol": s, "name": s, "match_score": 10.0, "sentiment_score": v}
                     for s, v in sym_scores.items()],
    }


def body(arts, found=None, page=1):
    return {"meta": {"found": found if found is not None else len(arts), "returned": len(arts),
                     "limit": 3, "page": page}, "data": arts}


class FakeClient:
    """pages: {(symbol, page): body 或 Exception}；沒列到的視為空結果。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def news_all(self, symbol, published_after, language="en", page=1):
        self.calls.append((symbol, page, published_after))
        r = self.pages.get((symbol, page), body([], found=0))
        if isinstance(r, Exception):
            raise r
        return r


def run(tmp_path, client, symbols, **kw):
    return cm.collect(tmp_path, now=NOW, client=client, symbols=symbols, **kw)


def read(tmp_path, name):
    return json.loads((tmp_path / "data" / "news" / name).read_text(encoding="utf-8"))


def test_no_key_skipped(tmp_path):
    class S:
        marketaux_api_token = ""

    out = cm.collect(tmp_path, now=NOW, settings=S())
    assert out == {"status": "skipped"}
    assert not (tmp_path / "data").exists()


def test_dry_run_no_request_no_write(tmp_path):
    fc = FakeClient({})
    out = run(tmp_path, fc, ["tsm", "NVDA"], dry_run=True)
    assert out["status"] == "dry_run" and out["symbols"] == ["TSM", "NVDA"]
    assert out["planned_requests"] == 4
    assert fc.calls == [] and not (tmp_path / "data").exists()


def test_published_after_winter_summer_weekend():
    assert cm.published_after(dt.date(2026, 11, 24)) == "2026-11-23T21:00:00"  # 冬令 EST
    assert cm.published_after(dt.date(2026, 7, 14)) == "2026-07-13T20:00:00"  # 夏令 EDT
    assert cm.published_after(dt.date(2026, 11, 23)) == "2026-11-20T21:00:00"  # 週一 -> 上週五


def test_max_two_pages_per_symbol(tmp_path):
    three = [art(f"a{i}", {"TSM": 0.1}) for i in range(3)]
    fc = FakeClient({("TSM", 1): body(three, found=20), ("TSM", 2): body(three, found=20, page=2),
                     ("TSM", 3): body(three, found=20, page=3)})
    run(tmp_path, fc, ["TSM"])
    assert [(s, p) for s, p, _ in fc.calls] == [("TSM", 1), ("TSM", 2)]
    assert fc.calls[0][2] == "2026-11-23T21:00:00"


def test_stops_when_no_next_page(tmp_path):
    fc = FakeClient({("TSM", 1): body([art("a", {"TSM": 0.2}), art("b", {"TSM": 0.4})], found=2)})
    run(tmp_path, fc, ["TSM"])
    assert len(fc.calls) == 1


def test_only_own_symbol_scores(tmp_path):
    fc = FakeClient({("TSM", 1): body([art("a", {"TSM": 0.5, "NVDA": -0.9}),
                                       art("b", {"NVDA": -0.8})], found=2)})
    out = run(tmp_path, fc, ["TSM"])
    row = out["symbols"]["TSM"]
    assert row["count"] == 1 and row["avg"] == 0.5


def test_sentiment_table_values(tmp_path):
    p1 = body([art("a", {"NVDA": 0.6}), art("b", {"NVDA": -0.2}), art("c", {"NVDA": 0.0})], found=5)
    p2 = body([art("c", {"NVDA": 0.0}), art("d", {"NVDA": 0.4})], found=5, page=2)  # c 重複
    out = run(tmp_path, FakeClient({("NVDA", 1): p1, ("NVDA", 2): p2}), ["NVDA"])
    row = out["symbols"]["NVDA"]
    assert row["status"] == "ok" and row["count"] == 4
    assert row["avg"] == 0.2  # (0.6-0.2+0+0.4)/4
    assert (row["positive"], row["negative"], row["neutral"]) == (2, 1, 1)


def test_no_data_symbol_is_marked_not_zero(tmp_path):
    fc = FakeClient({("TSM", 1): body([art("a", {"TSM": 0.3})], found=1)})
    out = run(tmp_path, fc, ["TSM", "MU"])
    assert out["symbols"]["MU"]["status"] == "no_data" and out["symbols"]["MU"]["avg"] is None
    saved = read(tmp_path, f"marketaux_sentiment_{DAY}.json")
    assert saved["symbols"]["MU"]["avg"] is None and saved["symbols"]["MU"]["status"] == "no_data"


def test_sentiment_file_written(tmp_path):
    fc = FakeClient({("TSM", 1): body([art("a", {"TSM": 0.3})], found=1)})
    out = run(tmp_path, fc, ["TSM"])
    saved = read(tmp_path, f"marketaux_sentiment_{DAY}.json")
    assert saved["date"] == DAY and saved["published_after"] == "2026-11-23T21:00:00"
    assert saved["source"] == "marketaux" and "不進交易規則" in saved["note"]
    assert saved["symbols"]["TSM"]["avg"] == 0.3
    assert out["sentiment_path"].endswith(f"marketaux_sentiment_{DAY}.json")
    assert not list((tmp_path / "data" / "news").glob("*.tmp"))


def test_news_merge_dedup_on_rerun(tmp_path):
    pages = {("TSM", 1): body([art("a", {"TSM": 0.3}), art("b", {"TSM": 0.1})], found=2)}
    run(tmp_path, FakeClient(pages), ["TSM"])
    pages2 = {("TSM", 1): body([art("b", {"TSM": 0.1}), art("c", {"TSM": 0.2})], found=2)}
    out = run(tmp_path, FakeClient(pages2), ["TSM"])
    news = read(tmp_path, f"api_marketaux_{DAY}.json")
    assert [n["url"] for n in news] == ["https://x.test/a", "https://x.test/b", "https://x.test/c"]
    assert out["total_items"] == 3 and news[0]["feed"] == "marketaux:x.test"


def test_quota_exceeded_stops(tmp_path):
    fc = FakeClient({("TSM", 1): body([art("a", {"TSM": 0.3})], found=1),
                     ("NVDA", 1): QuotaExceeded("marketaux", "daily", 90)})
    out = run(tmp_path, fc, ["TSM", "NVDA", "AMD", "MU"])
    assert out["status"] == "quota_exceeded"
    assert [s for s, _, _ in fc.calls] == ["TSM", "NVDA"]  # AMD、MU 不再送
    assert out["requests"] == 1
    assert out["symbols"]["NVDA"]["status"] == "error"
    assert "AMD" not in out["symbols"]
    assert cm.main  # 入口存在


class Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text

    def json(self):
        raise ValueError


class Sess:
    def __init__(self, status, text):
        self.resp, self.calls = Resp(status, text), 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        return self.resp


def _real_client(tmp_path, status, text):
    sess = Sess(status, text)
    return MarketauxClient(KEY, sess, QuotaTracker("marketaux", daily_limit=90, directory=tmp_path / "q")), sess


def test_http_402_stops_and_redacts_key(tmp_path):
    client, sess = _real_client(tmp_path, 402, f"usage limit reached api_token={KEY}")
    out = run(tmp_path, client, ["TSM", "NVDA"])
    assert out["status"] == "quota_exceeded" and sess.calls == 1
    assert KEY not in json.dumps(out, ensure_ascii=False)
    assert out["sentiment_path"] is None
    assert not (tmp_path / "data" / "news").exists()


def test_http_429_stops(tmp_path):
    client, sess = _real_client(tmp_path, 429, "too many")
    out = run(tmp_path, client, ["TSM", "NVDA"])
    assert out["status"] == "rate_limited" and sess.calls == 1


def test_partial_error_keeps_other_symbols_and_previous(tmp_path):
    # 先有舊資料
    run(tmp_path, FakeClient({("NVDA", 1): body([art("o", {"NVDA": 0.9})], found=1)}), ["NVDA"])
    fc = FakeClient({("TSM", 1): body([art("a", {"TSM": 0.3})], found=1),
                     ("NVDA", 1): SourceError("marketaux", 500, "boom")})
    out = run(tmp_path, fc, ["TSM", "NVDA", "MU"])
    assert out["status"] == "partial" and "NVDA" in out["errors"]
    assert len(fc.calls) == 3  # 非額度錯誤不中斷
    saved = read(tmp_path, f"marketaux_sentiment_{DAY}.json")["symbols"]
    assert saved["TSM"]["avg"] == 0.3
    assert saved["NVDA"]["avg"] == 0.9  # 舊資料不被錯誤覆蓋
    assert saved["MU"]["status"] == "no_data"


def test_main_parses_symbols_and_dry_run(tmp_path, capsys):
    rc = cm.main(["--dry-run", "--symbols", "tsm,amd", "--root", str(tmp_path)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "dry_run" and out["symbols"] == ["TSM", "AMD"]

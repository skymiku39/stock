import datetime as dt
import json
from types import SimpleNamespace

from bot.free_sources import collect_finnhub as cf
from bot.free_sources.base import SourceError
from bot.free_sources.quota import TAIPEI, QuotaExceeded

NOW = dt.datetime(2026, 11, 24, 8, 0, tzinfo=TAIPEI)  # 週二
KEY = "SECRET-KEY-999"


def row(i, headline=None, url=None, related="NVDA"):
    return {"category": "company", "datetime": 1732400000 + i, "headline": headline or f"h{i}",
            "id": i, "related": related, "source": "Reuters", "summary": "s",
            "url": url if url is not None else f"http://u/{i}"}


class FakeClient:
    def __init__(self, company=None, general=None, fail=(), quota_at=None):
        self.company = company or {}
        self.general = general if general is not None else []
        self.fail = set(fail)
        self.quota_at = quota_at
        self.calls = []

    def company_news(self, symbol, a, b):
        if self.quota_at is not None and len(self.calls) >= self.quota_at:
            raise QuotaExceeded("finnhub", "daily", 1)
        self.calls.append(("company", symbol, a, b))
        if symbol in self.fail:
            raise SourceError("finnhub", 500, "boom")
        return self.company.get(symbol, [])

    def general_news(self, min_id=None):
        if self.quota_at is not None and len(self.calls) >= self.quota_at:
            raise QuotaExceeded("finnhub", "daily", 1)
        self.calls.append(("general",))
        return self.general


def out(tmp_path, day="2026-11-24"):
    return tmp_path / "data" / "news" / f"api_finnhub_{day}.json"


def test_a1_skipped_without_key(tmp_path):
    r = cf.collect(tmp_path, now=NOW, settings=SimpleNamespace(finnhub_api_key=" "))
    assert r == {"status": "skipped"}
    assert not (tmp_path / "data").exists()


def test_a2_default_symbols_and_requests(tmp_path):
    syms = cf.default_symbols()
    assert len(syms) == 14 and "NVDA" in syms and "TSM" in syms and not any(s.isdigit() for s in syms)
    c = FakeClient()
    r = cf.collect(tmp_path, now=NOW, client=c)
    assert r["requests"] == 15
    assert [x[1] for x in c.calls if x[0] == "company"] == syms
    assert c.calls[-1] == ("general",)


def test_a3_date_range():
    d = dt.date
    assert cf.previous_weekday(d(2026, 11, 24)) == d(2026, 11, 23)  # 週二 -> 週一
    assert cf.previous_weekday(d(2026, 11, 23)) == d(2026, 11, 20)  # 週一 -> 週五
    assert cf.previous_weekday(d(2026, 11, 22)) == d(2026, 11, 20)  # 週日 -> 週五
    assert cf.previous_weekday(d(2026, 11, 21)) == d(2026, 11, 20)  # 週六 -> 週五


def test_a3_range_passed_to_client(tmp_path):
    c = FakeClient()
    cf.collect(tmp_path, now=NOW, client=c, symbols=["NVDA"])
    assert c.calls[0] == ("company", "NVDA", "2026-11-23", "2026-11-24")


def test_a4_output_format(tmp_path):
    c = FakeClient(company={"NVDA": [row(1)], "TSM": [row(2, related="TSM")]},
                   general=[row(3, related="SPY,QQQ")])
    r = cf.collect(tmp_path, now=NOW, client=c, symbols=["nvda", "TSM"])
    data = json.loads(out(tmp_path).read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 3
    assert [d["feed"] for d in data] == ["finnhub:NVDA", "finnhub:TSM", "finnhub:general"]
    assert data[0]["us_tickers"] == ["NVDA"] and data[2]["us_tickers"] == ["SPY", "QQQ"]
    for k in ("title", "url", "published_at"):
        assert data[0][k]
    assert r["counts"] == {"NVDA": 1, "TSM": 1, "general": 1}
    assert r["path"] == str(out(tmp_path)) and r["requests"] == 3 and r["status"] == "ok"


def test_a5_merge_with_existing_and_idempotent(tmp_path):
    p = out(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps([{"title": "old", "url": "http://u/1"}, {"title": "keep", "url": ""}]),
                 encoding="utf-8")
    c = FakeClient(company={"NVDA": [row(1), row(2)]})
    cf.collect(tmp_path, now=NOW, client=c, symbols=["NVDA"])
    data = json.loads(p.read_text(encoding="utf-8"))
    assert [d["title"] for d in data] == ["old", "keep", "h2"]
    cf.collect(tmp_path, now=NOW, client=c, symbols=["NVDA"])
    assert len(json.loads(p.read_text(encoding="utf-8"))) == 3


def test_a5_title_dedup_when_no_url(tmp_path):
    c = FakeClient(company={"NVDA": [row(1, headline="same", url=""), row(2, headline="same", url="")]})
    cf.collect(tmp_path, now=NOW, client=c, symbols=["NVDA"])
    assert len(json.loads(out(tmp_path).read_text(encoding="utf-8"))) == 1


def test_a6_dedup_within_batch(tmp_path):
    c = FakeClient(company={"NVDA": [row(1)], "AMD": [row(1, related="AMD")]})
    r = cf.collect(tmp_path, now=NOW, client=c, symbols=["NVDA", "AMD"])
    assert r["new_items"] == 2 and r["total_items"] == 1


def test_a7_atomic_no_temp_and_corrupt_existing(tmp_path):
    p = out(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text('{"not": "a list"}', encoding="utf-8")
    cf.collect(tmp_path, now=NOW, client=FakeClient(company={"NVDA": [row(1)]}), symbols=["NVDA"])
    assert len(json.loads(p.read_text(encoding="utf-8"))) == 1
    assert [f.name for f in p.parent.iterdir()] == [p.name]


def test_a7b_no_items_no_write(tmp_path):
    r = cf.collect(tmp_path, now=NOW, client=FakeClient(), symbols=["NVDA"])
    assert r["path"] is None and not out(tmp_path).exists()
    p = out(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text('[{"title":"x","url":"u"}]', encoding="utf-8")
    cf.collect(tmp_path, now=NOW, client=FakeClient(), symbols=["NVDA"])
    assert json.loads(p.read_text(encoding="utf-8")) == [{"title": "x", "url": "u"}]


def test_a8_source_error_continues(tmp_path):
    c = FakeClient(company={"AMD": [row(1, related="AMD")]}, fail=["NVDA"])
    r = cf.collect(tmp_path, now=NOW, client=c, symbols=["NVDA", "AMD"])
    assert "NVDA" in r["errors"] and r["counts"]["AMD"] == 1 and r["status"] == "partial"
    assert r["requests"] == 3  # NVDA、AMD、general 都有嘗試


def test_a9_quota_stops_but_writes_collected(tmp_path):
    c = FakeClient(company={"NVDA": [row(1)]}, quota_at=1)
    r = cf.collect(tmp_path, now=NOW, client=c, symbols=["NVDA", "AMD", "TSM"])
    assert r["status"] == "quota_exceeded" and r["requests"] == 1
    assert len(c.calls) == 1 and "AMD" in r["errors"]
    assert len(json.loads(out(tmp_path).read_text(encoding="utf-8"))) == 1


def test_a10_summary_has_no_key(tmp_path):
    class Leaky(FakeClient):
        def company_news(self, *a):
            raise SourceError("finnhub", 500, f"bad token={KEY}")

    r = cf.collect(tmp_path, now=NOW, client=Leaky(), symbols=["NVDA"])
    assert set(r) >= {"counts", "errors", "requests", "path"}
    assert KEY not in json.dumps(r)


def test_a11_dry_run(tmp_path, capsys):
    c = FakeClient()
    r = cf.collect(tmp_path, now=NOW, client=c, symbols=["NVDA"], dry_run=True)
    assert r["status"] == "dry_run" and r["planned_requests"] == 2 and c.calls == []
    assert not (tmp_path / "data").exists()


def test_a12_main_dry_run(tmp_path, capsys):
    rc = cf.main(["--dry-run", "--root", str(tmp_path), "--symbols", "NVDA"])
    printed = capsys.readouterr().out
    assert rc == 0 and json.loads(printed)["symbols"] == ["NVDA"]
    assert not (tmp_path / "data").exists()


def test_quota_stored_under_root(tmp_path, monkeypatch):
    class Sess:
        def __init__(self):
            self.n = 0

        def get(self, url, params=None, headers=None, timeout=None):
            self.n += 1
            return SimpleNamespace(status_code=200, json=lambda: [row(self.n)], text="")

    import bot.free_sources.collect_finnhub as mod

    real = mod.FinnhubClient
    monkeypatch.setattr(mod, "FinnhubClient", lambda key, quota=None: real(key, Sess(), quota))
    r = cf.collect(tmp_path, now=NOW, settings=SimpleNamespace(finnhub_api_key=KEY), symbols=["NVDA"])
    assert r["requests"] == 2
    q = json.loads((tmp_path / "data" / "api_quota" / "finnhub.json").read_text())
    assert q["count"] == 2

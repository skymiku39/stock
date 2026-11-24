"""FMP 財報收集（離線；假 client，不連網）。對應 docs/free-sources/fmp.md 驗收標準。"""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from bot.free_sources import collect_fmp as cf
from bot.free_sources.base import SourceError
from bot.free_sources.quota import TAIPEI, QuotaExceeded, QuotaTracker

KEY = "SECRET-KEY-123"
NOW = dt.datetime(2026, 11, 24, 10, 0, tzinfo=TAIPEI)  # 週二


def _income(period, n, currency="USD"):
    step = 4 if period == "quarter" else 1
    rows = []
    for i in range(n):
        rows.append({
            "date": f"{2026 - (i // step)}-{12 - (i % step) * 3:02d}-31" if period == "quarter" else f"{2026 - i}-12-31",
            "reportedCurrency": currency, "revenue": 1000 - i * 100, "grossProfit": (1000 - i * 100) * (0.6 - i * 0.01),
            "eps": 2.0 - i * 0.1, "epsDiluted": 1.9 - i * 0.1,
        })
    return rows


class FakeClient:
    def __init__(self, currency=None, fail=None, quota_after=None, tracker=None):
        self.calls = []
        self.currency = currency or {}
        self.fail = fail or {}
        self.quota_after = quota_after
        self.tracker = tracker

    def _do(self, kind, symbol, period, limit):
        if self.tracker:
            self.tracker.consume()
        if self.quota_after is not None and len(self.calls) >= self.quota_after:
            raise QuotaExceeded("fmp", "daily", self.quota_after)
        self.calls.append((kind, symbol, period, limit))
        if symbol in self.fail:
            raise SourceError("fmp", 404, "no data")
        n = limit
        if kind == "income":
            return _income(period, n, self.currency.get(symbol, "USD"))
        if kind == "cashflow":
            return [{"date": r["date"], "freeCashFlow": 500 - i * 50, "operatingCashFlow": 600, "capitalExpenditure": -100}
                    for i, r in enumerate(_income(period, n))]
        return [{"date": r["date"], "totalAssets": 9000, "totalDebt": 100} for r in _income(period, n)]

    def income_statement(self, s, period="annual", limit=5):
        return self._do("income", s, period, limit)

    def balance_sheet_statement(self, s, period="annual", limit=5):
        return self._do("balance", s, period, limit)

    def cash_flow_statement(self, s, period="annual", limit=5):
        return self._do("cashflow", s, period, limit)


def _file(root, sym):
    return root / "data" / "us_fundamentals" / f"{sym}.json"


def test_collects_six_requests_and_writes_file(tmp_path):
    c = FakeClient()
    res = cf.collect(tmp_path, c, ["NVDA"], now=NOW)
    assert res["fetched"] == ["NVDA"] and len(c.calls) == 6
    d = json.loads(_file(tmp_path, "NVDA").read_text(encoding="utf-8"))
    assert d["symbol"] == "NVDA" and d["currency"] == "USD" and d["fetched_at"].startswith("2026-11-24")
    for stmt in ("income", "balance", "cashflow"):
        assert len(d["statements"][stmt]["annual"]) == 5
        assert len(d["statements"][stmt]["quarter"]) == 5
    dates = [r["date"] for r in d["statements"]["income"]["annual"]]
    assert dates == sorted(dates, reverse=True)


def test_atomic_write_keeps_old_file_on_failure(tmp_path, monkeypatch):
    p = _file(tmp_path, "NVDA")
    p.parent.mkdir(parents=True)
    p.write_text('{"old": true}', encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(cf.os, "replace", boom)
    with pytest.raises(OSError):
        cf._atomic_write_json(p, {"new": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"old": True}
    assert [f.name for f in p.parent.iterdir()] == ["NVDA.json"]


def test_no_key_skipped(tmp_path, monkeypatch, capsys):
    import bot.config as config

    monkeypatch.setattr(config, "Settings", lambda: SimpleNamespace(fmp_api_key=""))
    assert cf.main(["--symbols", "NVDA", "--root", str(tmp_path)]) == 0
    assert "skipped" in capsys.readouterr().out
    assert not (tmp_path / "data").exists()


def test_single_symbol_error_continues(tmp_path):
    c = FakeClient(fail={"AMD"})
    res = cf.collect(tmp_path, c, ["NVDA", "AMD", "AAPL"], now=NOW)
    assert res["fetched"] == ["NVDA", "AAPL"] and "AMD" in res["errors"]
    assert not _file(tmp_path, "AMD").exists()


def test_quota_exceeded_stops(tmp_path):
    c = FakeClient(quota_after=8)  # 第 2 檔中途超限
    res = cf.collect(tmp_path, c, ["NVDA", "AMD", "AAPL"], now=NOW)
    assert res["quota_stopped"] and res["fetched"] == ["NVDA"]
    assert res["not_attempted"] == ["AMD", "AAPL"]
    assert not _file(tmp_path, "AMD").exists() and len(c.calls) == 8


def test_skip_if_fetched_this_week(tmp_path):
    cf.collect(tmp_path, FakeClient(), ["NVDA"], now=NOW)
    c = FakeClient()
    res = cf.collect(tmp_path, c, ["NVDA"], now=NOW + dt.timedelta(days=3))  # 週五，同週
    assert res["skipped_week"] == ["NVDA"] and c.calls == []


def test_force_refetches(tmp_path):
    cf.collect(tmp_path, FakeClient(), ["NVDA"], now=NOW)
    c = FakeClient()
    res = cf.collect(tmp_path, c, ["NVDA"], force=True, now=NOW)
    assert res["fetched"] == ["NVDA"] and len(c.calls) == 6


def test_new_week_refetches(tmp_path):
    cf.collect(tmp_path, FakeClient(), ["NVDA"], now=NOW)
    c = FakeClient()
    res = cf.collect(tmp_path, c, ["NVDA"], now=NOW + dt.timedelta(days=7))
    assert res["fetched"] == ["NVDA"] and len(c.calls) == 6


def test_dry_run_makes_no_requests(tmp_path, capsys):
    c = FakeClient()
    res = cf.collect(tmp_path, c, ["NVDA", "AMD"], dry_run=True, now=NOW)
    assert c.calls == [] and res["requests_planned"] == 12 and not (tmp_path / "data").exists()
    assert cf.main(["--dry-run", "--symbols", "NVDA", "--root", str(tmp_path)]) == 0
    assert '"requests_planned": 6' in capsys.readouterr().out and not (tmp_path / "data").exists()


def test_symbols_option(tmp_path):
    c = FakeClient()
    cf.collect(tmp_path, c, ["nvda", " tsm "], now=NOW)
    assert {x[1] for x in c.calls} == {"NVDA", "TSM"}


def test_default_symbols_are_all_us_stocks():
    from bot.market_macro import DEFAULT_STOCKS

    syms = cf.default_symbols()
    assert syms == [r[0] for r in DEFAULT_STOCKS] and len(syms) == 14 and "TSM" in syms


def test_currency_twd_preserved(tmp_path):
    cf.collect(tmp_path, FakeClient(currency={"TSM": "TWD"}), ["TSM"], now=NOW)
    d = json.loads(_file(tmp_path, "TSM").read_text(encoding="utf-8"))
    assert d["currency"] == "TWD"
    assert cf.summarize(d)["currency"] == "TWD"


def test_summarize_trends(tmp_path):
    cf.collect(tmp_path, FakeClient(), ["NVDA"], now=NOW)
    s = cf.summarize(json.loads(_file(tmp_path, "NVDA").read_text(encoding="utf-8")))
    a = s["annual"]
    assert a["revenue"]["latest"] == 1000 and a["revenue"]["yoy_pct"] == pytest.approx(11.11)
    assert a["gross_margin_pct"]["latest"] == pytest.approx(60.0)
    assert a["gross_margin_pct"]["change_pts"] == pytest.approx(1.0)
    assert a["eps"]["latest"] == pytest.approx(1.9)
    assert a["free_cash_flow"]["latest"] == 500 and a["free_cash_flow"]["yoy_pct"] == pytest.approx(11.11)
    assert len(s["quarter"]["revenue"]["series"]) == 5 and s["quarter"]["revenue"]["yoy_pct"] is not None


def test_summarize_handles_missing():
    s = cf.summarize({"symbol": "X", "statements": {"income": {"annual": [{"date": "2025-12-31", "revenue": None}]}}})
    assert s["annual"]["revenue"]["latest"] is None and s["annual"]["revenue"]["yoy_pct"] is None
    assert s["annual"]["gross_margin_pct"]["latest"] is None
    assert cf.summarize({})["annual"]["eps"]["series"] == []


def test_raw_responses_registered_without_key(tmp_path):
    from bot.source_registry import SourceRegistry

    with SourceRegistry(tmp_path) as reg:
        cf.collect(tmp_path, FakeClient(), ["NVDA"], now=NOW, registry=reg)
        rows = reg.db.conn.execute("SELECT content_path, url FROM source_documents WHERE source='fmp'").fetchall()
    assert len(rows) == 6
    for path, url in rows:
        assert KEY not in url and "apikey" not in url.lower()
        assert (tmp_path / path).is_file()


def test_full_run_within_daily_quota(tmp_path):
    tracker = QuotaTracker("fmp", daily_limit=230, directory=tmp_path / "q", now=lambda: NOW)
    c = FakeClient(tracker=tracker)
    res = cf.collect(tmp_path, c, None, now=NOW)
    assert len(res["fetched"]) == 14 and len(c.calls) == 84 <= 230
    assert tracker.used_today() == 84 and not res["quota_stopped"]

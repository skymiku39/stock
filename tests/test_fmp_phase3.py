"""FMP 第三階段：排程、402 分類、研究頁讀取點（離線）。對應 docs/free-sources/fmp.md 驗收標準 14–21。"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from bot.config import Settings
from bot.free_sources import collect_fmp as cf
from bot.free_sources import finnhub, fmp, marketaux
from bot.free_sources.base import SourceError
from bot.free_sources.quota import TAIPEI, QuotaTracker

KEY = "SECRET-KEY-123"
NOW = dt.datetime(2026, 11, 24, 10, 0, tzinfo=TAIPEI)  # 週二
LIMIT_402 = ("Premium Query Parameter: 'Special Parameters : The values for 'limit' must be between 0 and 5 "
             "based on your current subscription. Please visit our subscription page to upgrade your plan")


class FakeResp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        return self.resp


def _quota(tmp_path, name):
    return QuotaTracker(name, directory=tmp_path)


def _rows(period, n=5):
    out = []
    for i in range(n):
        date = f"{2026 - i // 4}-{12 - (i % 4) * 3:02d}-28" if period == "quarter" else f"{2026 - i}-12-31"
        out.append({"date": date, "reportedCurrency": "USD", "revenue": 1000 - i * 100,
                    "grossProfit": (1000 - i * 100) * 0.5, "epsDiluted": 2.0 - i * 0.1,
                    "freeCashFlow": 300 - i * 10})
    return out


class FakeClient:
    """假 FMP client；fail 對應代號 -> SourceError kind。"""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}

    def _do(self, symbol, period, limit):
        self.calls.append((symbol, period, limit))
        if symbol in self.fail:
            raise SourceError("fmp", 402, "bad", self.fail[symbol])
        return _rows(period, limit)

    def income_statement(self, s, period="annual", limit=5):
        return self._do(s, period, limit)

    balance_sheet_statement = income_statement
    cash_flow_statement = income_statement


def _settings(**kw):
    base = dict(symbols=["2330"], _env_file=None)
    base.update(kw)
    return Settings(**base)  # type: ignore[call-arg]


# ---------------- 14. 排程工作與開關 ----------------
def test_scheduler_fmp_job_built_and_toggle(tmp_path):
    from bot.scheduler import Scheduler

    sch = Scheduler(_settings(), project_root=tmp_path, dry_run=True)
    job = next(j for j in sch.jobs if j.name == "fmp_fundamentals")
    assert job.module == "bot.free_sources.collect_fmp"
    assert job.extra_args == ["--root", str(tmp_path)]
    assert job.market_hours_only is False and job.run_once_per_day is False
    assert job.interval_min == 360

    off = Scheduler(_settings(scheduler_fmp_enabled=False), project_root=tmp_path, dry_run=True)
    assert all(j.name != "fmp_fundamentals" for j in off.jobs)

    custom = Scheduler(_settings(scheduler_fmp_interval_min=720), project_root=tmp_path, dry_run=True)
    assert next(j for j in custom.jobs if j.name == "fmp_fundamentals").interval_min == 720


# ---------------- 15. 排程一週只抓一輪 ----------------
def test_scheduled_runs_fetch_once_per_week(tmp_path):
    syms = ["NVDA", "TSM"]
    first = FakeClient()
    cf.collect(tmp_path, first, syms, now=NOW)
    assert len(first.calls) == 12
    again = FakeClient()  # 6 小時後排程再叫一次（同週）
    res = cf.collect(tmp_path, again, syms, now=NOW + dt.timedelta(hours=6))
    assert again.calls == [] and res["skipped_week"] == syms and res["requests_planned"] == 0
    weekend = FakeClient()  # 週日仍同一個 ISO 週
    cf.collect(tmp_path, weekend, syms, now=NOW + dt.timedelta(days=5))
    assert weekend.calls == []
    next_week = FakeClient()
    cf.collect(tmp_path, next_week, syms, now=NOW + dt.timedelta(days=6))  # 下週一
    assert len(next_week.calls) == 12


# ---------------- 16. FMP 402 分類 ----------------
def _fmp_error(tmp_path, text):
    c = fmp.FmpClient(KEY, FakeSession(FakeResp(402, text=text)), _quota(tmp_path, "fmp"))
    with pytest.raises(SourceError) as ei:
        c.income_statement("NVDA", period="quarter", limit=8)
    assert KEY not in str(ei.value)
    return ei.value


def test_fmp_402_param_error_classified(tmp_path):
    err = _fmp_error(tmp_path, LIMIT_402)
    assert err.kind == "invalid_params" and err.status == 402


def test_fmp_402_plan_restricted(tmp_path):
    err = _fmp_error(tmp_path, "Restricted Endpoint: This endpoint is not available under your current subscription")
    assert err.kind == "plan_restricted"


def test_fmp_402_other_is_quota(tmp_path):
    assert _fmp_error(tmp_path, "Limit Reach . Please upgrade your plan").kind == "quota_exhausted"
    assert _fmp_error(tmp_path, "").kind == "quota_exhausted"


# ---------------- 17. 其他家不受影響 ----------------
def test_other_sources_402_still_quota(tmp_path):
    c = marketaux.MarketauxClient(KEY, FakeSession(FakeResp(402, text=LIMIT_402)), _quota(tmp_path, "mx"))
    with pytest.raises(SourceError) as ei:
        c.news_all(["TSM"], "2026-11-23T21:00")
    assert ei.value.kind == "quota_exhausted"
    f = finnhub.FinnhubClient(KEY, FakeSession(FakeResp(402, text=LIMIT_402)), _quota(tmp_path, "fh"))
    with pytest.raises(SourceError) as ei2:
        f.company_news("NVDA", "2026-11-21", "2026-11-24")
    assert ei2.value.kind == "quota_exhausted"


# ---------------- 18. invalid_params 立即停止 ----------------
def test_invalid_params_stops_collection(tmp_path):
    c = FakeClient(fail={"AMD": "invalid_params"})
    res = cf.collect(tmp_path, c, ["NVDA", "AMD", "AAPL"], now=NOW)
    assert res["fetched"] == ["NVDA"] and res["stopped_reason"] == "invalid_params"
    assert res["not_attempted"] == ["AAPL"] and "invalid_params" in res["errors"]["AMD"]
    assert all(sym != "AAPL" for sym, _, _ in c.calls)
    # plan_restricted 只影響該檔，繼續下一檔
    c2 = FakeClient(fail={"AMD": "plan_restricted"})
    res2 = cf.collect(tmp_path / "b", c2, ["NVDA", "AMD", "AAPL"], now=NOW)
    assert res2["fetched"] == ["NVDA", "AAPL"] and res2["stopped_reason"] == ""


# ---------------- 19. load_summary ----------------
def test_load_summary_compact_and_stale(tmp_path):
    assert cf.load_summary(tmp_path, "NVDA") is None
    cf.collect(tmp_path, FakeClient(), ["NVDA"], now=NOW)
    s = cf.load_summary(tmp_path, "nvda", now=NOW + dt.timedelta(days=3))
    assert s["symbol"] == "NVDA" and s["currency"] == "USD" and s["stale"] is False
    assert s["annual"]["revenue"]["latest"] == 1000.0 and s["annual"]["revenue"]["yoy_pct"] == pytest.approx(11.11)
    assert s["quarter"]["revenue"]["yoy_pct"] == pytest.approx(66.67)
    assert "series" not in json.dumps(s)
    assert "不進交易規則" in s["note"]
    assert cf.load_summary(tmp_path, "NVDA", now=NOW + dt.timedelta(days=15))["stale"] is True
    bad = cf.data_dir(tmp_path) / "BAD.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cf.load_summary(tmp_path, "BAD") is None


# ---------------- 20. summaries_for_tw ----------------
def test_summaries_for_tw_adr_first(tmp_path):
    cf.collect(tmp_path, FakeClient(), ["TSM", "NVDA", "AAPL"], now=NOW)
    related = [{"us_ticker": "AAPL"}, {"us_ticker": "NVDA"}, {"us_ticker": "AMD"}, {"us_ticker": "TSM"}]
    out = cf.summaries_for_tw(tmp_path, "2330", related, now=NOW)
    assert [(x["symbol"], x["relation"]) for x in out] == [
        ("TSM", "adr"), ("AAPL", "supply_chain"), ("NVDA", "supply_chain")]  # AMD 無檔、TSM 不重複
    assert cf.summaries_for_tw(tmp_path, "2330", related, limit=1, now=NOW)[0]["symbol"] == "TSM"
    assert cf.summaries_for_tw(tmp_path, "9999", None, now=NOW) == []


# ---------------- 21. 研究頁快照 ----------------
def test_ticker_snapshot_includes_us_fundamentals(tmp_path, monkeypatch):
    from bot import ticker_view

    cf.collect(tmp_path, FakeClient(), ["TSM"], now=NOW)

    def no_network(*a, **kw):
        raise AssertionError("讀取點不可呼叫 FMP")

    monkeypatch.setattr(fmp.FmpClient, "_get", no_network)
    snap = ticker_view.build_snapshot(
        "2330", tmp_path, auto_fill_missing=False, auto_llm=False, macro_cache_only=True,
        allow_live_sources=False,
    )
    assert snap.us_fundamentals and snap.us_fundamentals[0]["symbol"] == "TSM"
    assert snap.us_fundamentals[0]["relation"] == "adr"
    assert ticker_view.snapshot_to_dict(snap)["us_fundamentals"] == snap.us_fundamentals


# ---------------- 20. 代號不在方案內（2026-11-24 排程實測 ASML） ----------------
ASML_402 = ("Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available "
            "under your current subscription please visit our subscription page to upgrade your plan")


def test_fmp_402_symbol_not_in_plan_is_plan_restricted(tmp_path):
    assert _fmp_error(tmp_path, ASML_402).kind == "plan_restricted"
    assert _fmp_error(tmp_path, LIMIT_402).kind == "invalid_params"


def test_main_exit_zero_when_only_plan_restricted(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "collect", lambda *a, **k: {
        "fetched": ["NVDA"], "errors": {"ASML": "plan_restricted: x"}, "skipped_week": [],
        "not_attempted": [], "requests_planned": 6, "dry_run": False, "stopped_reason": ""})
    monkeypatch.setattr("bot.config.Settings", lambda *a, **k: type("S", (), {"fmp_api_key": "k"})())
    assert cf.main(["--root", str(tmp_path)]) == 0
    monkeypatch.setattr(cf, "collect", lambda *a, **k: {
        "fetched": [], "errors": {"AMD": "network: x"}, "skipped_week": [],
        "not_attempted": [], "requests_planned": 6, "dry_run": False, "stopped_reason": ""})
    assert cf.main(["--root", str(tmp_path)]) == 1

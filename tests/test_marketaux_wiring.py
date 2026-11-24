"""Marketaux 接線：排程工作、--scheduled 回傳碼、隔夜重點的 us_sentiment（離線，不連網）。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

from bot import overnight_brief as ob
from bot.config import Settings
from bot.free_sources import collect_marketaux as cm

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 24, 8, 0, tzinfo=TZ)


def _settings(**kw) -> Settings:
    base = dict(symbols=["2330"], _env_file=None)
    base.update(kw)
    return Settings(**base)  # type: ignore[call-arg]


def _jobs(tmp_path, **kw):
    from bot.scheduler import Scheduler

    return {j.name: j for j in Scheduler(_settings(**kw), project_root=tmp_path, dry_run=True).jobs}


# --- 排程 -----------------------------------------------------------------


def test_scheduler_job_needs_token(tmp_path):
    assert "marketaux_sentiment" not in _jobs(tmp_path)  # 沒有金鑰就不排
    assert "marketaux_sentiment" not in _jobs(tmp_path, marketaux_api_token="k",
                                              scheduler_marketaux_enabled=False)


def test_scheduler_job_runs_before_overnight_brief(tmp_path):
    jobs = _jobs(tmp_path, marketaux_api_token="k")
    job = jobs["marketaux_sentiment"]
    assert job.module == "bot.free_sources.collect_marketaux" and job.console_script == ""
    assert job.run_once_per_day and not job.market_hours_only
    assert job.extra_args == ["--root", str(tmp_path), "--scheduled"]
    assert (job.window_start, job.window_end) == (dt.time(7, 40), dt.time(7, 59))
    assert job.window_end < jobs["overnight_brief"].window_start


def test_scheduler_time_settings_parse_from_strings():
    s = _settings(scheduler_marketaux_time="07:15", scheduler_marketaux_end_time="07:45")
    assert (s.scheduler_marketaux_time, s.scheduler_marketaux_end_time) == (dt.time(7, 15), dt.time(7, 45))


def test_scheduled_flag_does_not_fail_on_limits(tmp_path, monkeypatch, capsys):
    for status, plain, scheduled in (("quota_exceeded", 1, 0), ("rate_limited", 1, 0),
                                     ("error", 1, 1), ("ok", 0, 0), ("skipped", 0, 0)):
        monkeypatch.setattr(cm, "collect", lambda *a, _s=status, **k: {"status": _s})
        assert cm.main(["--root", str(tmp_path)]) == plain, status
        assert cm.main(["--root", str(tmp_path), "--scheduled"]) == scheduled, status
    capsys.readouterr()


# --- 隔夜重點 ---------------------------------------------------------------


def _write_sentiment(root: Path, day: str, symbols: dict) -> None:
    path = root / "data" / "news" / f"marketaux_sentiment_{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": day, "published_after": "2026-11-23T21:00:00",
                                "source": "marketaux", "note": "n", "symbols": symbols}),
                    encoding="utf-8")


def _snap():
    q = SimpleNamespace(symbol="^GSPC", name="S&P 500", price=1.0, pct_change=0.5,
                        source_date="2026-11-23")
    return SimpleNamespace(fetched_at="", usdtwd=32.0, indices={"^GSPC": q}, stocks={},
                           adr_premiums=[])


def test_us_sentiment_missing_file(tmp_path):
    sec = ob.us_sentiment_section(tmp_path, NOW.date())
    assert sec["status"] == "missing" and sec["rows"] == []


def test_us_sentiment_no_data_shows_label_not_zero(tmp_path):
    _write_sentiment(tmp_path, "2026-11-24", {
        "TSM": {"status": "ok", "avg": 0.25, "count": 3, "positive": 2, "negative": 1, "neutral": 0},
        "MU": {"status": "no_data", "avg": None, "count": 0, "positive": 0, "negative": 0, "neutral": 0},
        "AMD": {"status": "error", "avg": None, "count": 0, "error": "x"},
    })
    sec = ob.us_sentiment_section(tmp_path, NOW.date())
    rows = {r["symbol"]: r for r in sec["rows"]}
    assert sec["status"] == "ok" and sec["published_after"] == "2026-11-23T21:00:00"
    assert rows["TSM"]["avg"] == 0.25 and rows["TSM"]["display"] == "+0.25（3 則：正 2／負 1）"
    for sym in ("MU", "AMD"):
        assert rows[sym]["avg"] is None and rows[sym]["display"] == "無資料"
    assert rows["AMD"]["status"] == "error"
    assert "不進交易規則" in sec["note"]


def test_us_sentiment_all_no_data(tmp_path):
    _write_sentiment(tmp_path, "2026-11-24", {"MU": {"status": "no_data", "avg": None, "count": 0}})
    sec = ob.us_sentiment_section(tmp_path, NOW.date())
    assert sec["status"] == "no_data" and sec["rows"][0]["display"] == "無資料"


def test_us_sentiment_only_reads_today(tmp_path):
    _write_sentiment(tmp_path, "2026-11-23", {"TSM": {"status": "ok", "avg": 0.5, "count": 1}})
    assert ob.us_sentiment_section(tmp_path, NOW.date())["status"] == "missing"


def test_build_brief_includes_us_sentiment(tmp_path):
    _write_sentiment(tmp_path, "2026-11-24", {"TSM": {"status": "ok", "avg": -0.1, "count": 1,
                                                      "positive": 0, "negative": 1, "neutral": 0}})
    brief = ob.build_brief(tmp_path, now=NOW, slot="preopen", macro_loader=_snap, notify=False)
    assert brief["us_sentiment"]["rows"][0]["display"] == "-0.10（1 則：正 0／負 1）"
    saved = json.loads((tmp_path / "data" / "overnight_brief" / "latest.json").read_text(encoding="utf-8"))
    assert saved["us_sentiment"]["status"] == "ok"


def test_build_brief_without_sentiment_file(tmp_path):
    brief = ob.build_brief(tmp_path, now=NOW, slot="preopen", macro_loader=_snap, notify=False)
    assert brief["us_sentiment"]["status"] == "missing"

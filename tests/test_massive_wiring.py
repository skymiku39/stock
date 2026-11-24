"""Massive 接線：排程工作與 --scheduled 回傳碼（離線，不連網）。"""
from __future__ import annotations

import datetime as dt

from bot.config import Settings
from bot.free_sources import collect_massive as cm
from bot.free_sources.quota import DEFAULT_LIMITS


def _settings(**kw) -> Settings:
    base = dict(symbols=["2330"], _env_file=None)
    base.update(kw)
    return Settings(**base)  # type: ignore[call-arg]


def _jobs(tmp_path, **kw):
    from bot.scheduler import Scheduler

    return {j.name: j for j in Scheduler(_settings(**kw), project_root=tmp_path, dry_run=True).jobs}


def test_scheduler_jobs_need_key_and_switch(tmp_path):
    assert "massive_news" not in _jobs(tmp_path)  # 沒有金鑰就不排
    jobs = _jobs(tmp_path, massive_api_key="k", scheduler_massive_enabled=False)
    assert "massive_news" not in jobs and "massive_news_update" not in jobs


def test_scheduler_jobs_run_before_each_overnight_brief(tmp_path):
    jobs = _jobs(tmp_path, massive_api_key="k")
    for name, brief in (("massive_news", "overnight_brief"),
                        ("massive_news_update", "overnight_brief_update")):
        job = jobs[name]
        assert job.module == "bot.free_sources.collect_massive" and job.console_script == ""
        assert job.run_once_per_day and not job.market_hours_only
        assert job.extra_args == ["--root", str(tmp_path), "--scheduled"]
        assert job.window_start < job.window_end < jobs[brief].window_start, name
    assert (jobs["massive_news"].window_start, jobs["massive_news"].window_end) == (dt.time(7, 50), dt.time(7, 59))
    assert (jobs["massive_news_update"].window_start,
            jobs["massive_news_update"].window_end) == (dt.time(8, 40), dt.time(8, 49))


def test_scheduler_time_settings_parse_from_strings():
    s = _settings(scheduler_massive_time="07:30", scheduler_massive_end_time="07:45",
                  scheduler_massive_update_time="08:30", scheduler_massive_update_end_time="08:45")
    assert (s.scheduler_massive_time, s.scheduler_massive_end_time) == (dt.time(7, 30), dt.time(7, 45))
    assert (s.scheduler_massive_update_time, s.scheduler_massive_update_end_time) == (dt.time(8, 30), dt.time(8, 45))


def test_scheduled_flag_does_not_fail_on_limits(tmp_path, monkeypatch, capsys):
    for status, plain, scheduled in (("quota", 2, 0), ("error", 2, 2), ("ok", 0, 0),
                                     ("skipped", 0, 0)):
        monkeypatch.setattr(cm, "collect_massive", lambda *a, _s=status, **k: {"status": _s})
        assert cm.main(["--root", str(tmp_path)]) == plain, status
        assert cm.main(["--root", str(tmp_path), "--scheduled"]) == scheduled, status
    capsys.readouterr()


def test_page_limit_is_free_plan_max():
    # 實測：limit=1000 可用、1001 回 400 Failed to validate 'max'。
    assert cm.PAGE_LIMIT == 1000


def test_quota_defaults_match_free_plan():
    # 實測（2026-11-24）：免費方案每分鐘 5 次，第 6 次回 429；保留 1 次餘裕。
    assert DEFAULT_LIMITS["massive"]["per_minute"] == 4

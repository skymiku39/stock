"""微笑情緒分數每日紀錄：假資料、tmp_path，不連網。"""
from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot import smile_mood_log as sml
from bot.rss_archive import archive_path

TZ = ZoneInfo("Asia/Taipei")
NOW = dt.datetime(2026, 11, 24, 9, 35, tzinfo=TZ)


def _settings(**kw) -> Settings:
    return Settings(symbols=["0050"], _env_file=None, **kw)


def _write_rss(root, negatives: int, positives: int = 0, collected=NOW) -> None:
    items = []
    for i in range(negatives):
        items.append({"title": f"台股重挫 {i}", "published_at": (NOW - dt.timedelta(minutes=10)).isoformat()})
    for i in range(positives):
        items.append({"title": f"台股大漲 {i}", "published_at": (NOW - dt.timedelta(minutes=10)).isoformat()})
    path = archive_path(root, NOW.date().isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"collected_at": (collected - dt.timedelta(minutes=5)).isoformat(),
                                "items": items}, ensure_ascii=False), encoding="utf-8")


def test_row_all_thresholds_reached(tmp_path) -> None:
    _write_rss(tmp_path, 12, 1)
    row = sml.run_log(tmp_path, _settings(), now=NOW, fetch_change=lambda: -2.0)
    assert row["news_negative"] == 12 and row["slot"] == "morning"
    assert row["news_negative_reached"] and row["news_score_reached"] and row["price_drop_reached"]
    assert row["pessimistic"] == {"both": True, "news": True, "price": True}
    assert row["thresholds"]["news_min_negative"] == 10
    assert sml.read_rows(sml.log_path(tmp_path)) == [row]


def test_row_news_only_and_price_only(tmp_path) -> None:
    _write_rss(tmp_path, 12)
    row = sml.run_log(tmp_path, _settings(), now=NOW, fetch_change=lambda: -0.5)
    assert row["pessimistic"] == {"both": False, "news": True, "price": False}
    row = sml.run_log(tmp_path, _settings(), now=NOW, fetch_change=lambda: -3.0, write=False)
    assert row["pessimistic"]["price"] is True


def test_missing_data_is_none_not_error(tmp_path) -> None:
    row = sml.run_log(tmp_path, _settings(), now=NOW, fetch_change=lambda: None)
    assert row["news_fresh"] is False and row["news_negative"] is None
    assert row["market_change_pct"] is None and row["price_drop_reached"] is None
    assert row["pessimistic"] == {"both": False, "news": False, "price": False}


def test_stale_news_not_counted(tmp_path) -> None:
    _write_rss(tmp_path, 12, collected=NOW - dt.timedelta(hours=5))
    row = sml.run_log(tmp_path, _settings(), now=NOW, fetch_change=lambda: -2.0)
    assert row["news_fresh"] is False and row["pessimistic"]["news"] is False


def test_same_day_slot_overwrites_other_slot_kept(tmp_path) -> None:
    _write_rss(tmp_path, 3)
    sml.run_log(tmp_path, _settings(), now=NOW, fetch_change=lambda: -1.0)
    sml.run_log(tmp_path, _settings(), now=NOW, fetch_change=lambda: -2.0)
    sml.run_log(tmp_path, _settings(), now=NOW.replace(hour=13, minute=0), fetch_change=lambda: 0.3)
    rows = sml.read_rows(sml.log_path(tmp_path))
    assert [(r["slot"], r["market_change_pct"]) for r in rows] == [("morning", -2.0), ("midday", 0.3)]


def test_summary_percentiles_and_triggers(tmp_path) -> None:
    path = sml.log_path(tmp_path)
    for day, (neg, score, chg) in enumerate([(2, 0.1, 0.5), (12, 0.8, -2.0), (11, 0.5, -0.2),
                                              (0, 0.0, -1.6)], start=1):
        sml.write_row(tmp_path, {"date": f"2026-11-{day:02d}", "slot": "morning",
                                 "news_negative": neg, "news_score": score,
                                 "market_change_pct": chg})
    s = sml.summarize(sml.read_rows(path), _settings())
    assert s["days"] == 4
    assert s["metrics"]["news_negative"]["median"] == 6.5
    assert s["metrics"]["market_change_pct"]["min"] == -2.0
    assert s["triggers"]["both"]["days"] == 1
    assert s["triggers"]["news"]["days"] == 2
    assert s["triggers"]["price"]["days"] == 2
    assert "已累積 4 天" in sml.format_summary(s)


def test_main_summary_empty(tmp_path, capsys) -> None:
    assert sml.main(["--root", str(tmp_path), "--summary"]) == 0
    assert "已累積 0 天" in capsys.readouterr().out


def test_default_does_not_enable_mood_sell() -> None:
    s = _settings()
    assert s.smile_mood_sell_enabled is False and s.scheduler_smile_mood_log_enabled is True


def test_scheduler_jobs_and_switch(tmp_path) -> None:
    from bot.scheduler import Scheduler

    def names(**kw):
        sch = Scheduler(_settings(**kw), project_root=tmp_path, dry_run=True)
        return {j.name: j for j in sch.jobs if j.module == "bot.smile_mood_log"}

    jobs = names()
    assert set(jobs) == {"smile_mood_log", "smile_mood_log_update"}
    a, b = jobs["smile_mood_log"], jobs["smile_mood_log_update"]
    assert a.run_once_per_day and (a.window_start, a.window_end) == (dt.time(9, 30), dt.time(10, 0))
    assert (b.window_start, b.window_end) == (dt.time(13, 0), dt.time(13, 25))
    assert a.extra_args == ["--root", str(tmp_path)]
    assert names(scheduler_smile_mood_log_enabled=False) == {}

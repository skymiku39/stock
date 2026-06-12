"""背景排程器與新掛牌 ETF no_data_yet 偵測的回歸測試。"""

from __future__ import annotations

import datetime as dt

from bot.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(symbols=["2330"], _env_file=None)
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Scheduler 任務建構
# ---------------------------------------------------------------------------


def test_scheduler_builds_both_jobs_by_default() -> None:
    from bot.scheduler import Scheduler

    sch = Scheduler(_settings(), dry_run=True)
    names = {j.name for j in sch.jobs}
    assert names == {"macro", "fundamentals", "research", "company", "history_fetch"}


def test_scheduler_skips_disabled_jobs() -> None:
    from bot.scheduler import Scheduler

    sch = Scheduler(
        _settings(
            scheduler_macro_interval_min=0,
            scheduler_fundamentals_interval_min=0,
            scheduler_research_interval_min=0,
            scheduler_company_interval_min=0,
            scheduler_history_fetch_interval_min=0,
        ),
        dry_run=True,
    )
    assert sch.jobs == []


def test_scheduler_research_extra_args_parsed() -> None:
    from bot.scheduler import Scheduler

    sch = Scheduler(_settings(scheduler_research_args="--no-brief --days 3"), dry_run=True)
    research = next(j for j in sch.jobs if j.name == "research")
    assert research.extra_args == ["--no-brief", "--days", "3"]


def test_scheduler_fundamentals_extra_args_parsed() -> None:
    from bot.scheduler import Scheduler

    sch = Scheduler(_settings(scheduler_fundamentals_args="--limit 1 --delay-seconds 0"), dry_run=True)
    fundamentals = next(j for j in sch.jobs if j.name == "fundamentals")
    assert fundamentals.extra_args == ["--limit", "1", "--delay-seconds", "0"]


def test_scheduler_history_fetch_extra_args_parsed() -> None:
    from bot.scheduler import Scheduler

    sch = Scheduler(
        _settings(scheduler_history_fetch_args="--once --batch-size 1 --delay 3"),
        dry_run=True,
    )
    history = next(j for j in sch.jobs if j.name == "history_fetch")
    assert history.extra_args == ["--once", "--batch-size", "1", "--delay", "3"]


def test_scheduler_adds_daytrade_llm_jobs_when_enabled() -> None:
    from bot.scheduler import Scheduler

    sch = Scheduler(
        _settings(
            scheduler_intraday_enabled=True,
            scheduler_nextday_draft_enabled=True,
            scheduler_nextday_update_enabled=True,
        ),
        dry_run=True,
    )
    names = {j.name for j in sch.jobs}
    assert {"intraday", "nextday_draft", "nextday_update"}.issubset(names)
    assert next(j for j in sch.jobs if j.name == "intraday").run_once_per_day is True


def test_scheduler_nextday_update_targets_today() -> None:
    from bot.scheduler import Scheduler

    sch = Scheduler(_settings(scheduler_nextday_update_enabled=True), dry_run=True)
    job = next(j for j in sch.jobs if j.name == "nextday_update")
    now = dt.datetime(2026, 6, 4, 2, 30)

    assert sch._job_extra_args(job, now)[-2:] == ["--target-date", "2026-06-04"]


def test_data_window_only_weekday_in_hours() -> None:
    from bot.scheduler import _is_data_window

    # 平日 (週一) 盤中
    monday_mid = dt.datetime(2026, 6, 1, 10, 0)
    assert _is_data_window(monday_mid) is True
    # 平日盤後 (晚上)
    monday_night = dt.datetime(2026, 6, 1, 20, 0)
    assert _is_data_window(monday_night) is False
    # 週六盤中時段 -> 仍 False
    saturday_mid = dt.datetime(2026, 6, 6, 10, 0)
    assert _is_data_window(saturday_mid) is False


def test_resolve_cmd_uses_console_script_or_module() -> None:
    from bot.scheduler import _resolve_cmd

    cmd = _resolve_cmd("stock-macro-update", "bot.macro_update", [])
    # 不論用 uv run 或 python -m，console-script 或 module 名稱都應出現其一
    assert "stock-macro-update" in cmd or "bot.macro_update" in cmd


# ---------------------------------------------------------------------------
# 新掛牌 ETF no_data_yet 偵測
# ---------------------------------------------------------------------------


def test_no_data_marker_detects_moneydj_empty_page() -> None:
    from bot.etf_holdings_fetcher import _looks_like_no_data

    assert _looks_like_no_data("主動安聯美國科技 持股狀況\n查無資料\n附註") is True
    assert _looks_like_no_data("No data available for this fund") is True


def test_no_data_marker_false_for_real_holdings() -> None:
    from bot.etf_holdings_fetcher import _looks_like_no_data

    text = "台積電 2330 25.3%\n聯發科 2454 8.1%\n鴻海 2317 5.0%"
    assert _looks_like_no_data(text) is False


def test_fundamentals_refresh_queue_success(tmp_path) -> None:
    from unittest.mock import patch

    from bot.fundamentals_fetcher import DividendRecord, FundamentalSnapshot
    from bot.fundamentals_refresh import (
        enqueue_refresh,
        load_refresh_queue,
        run_refresh_queue,
    )

    snap = FundamentalSnapshot(
        ticker="2330",
        dividends=[DividendRecord(ticker="2330", year=2025, cash_dividend=1.0)],
    )
    enqueue_refresh("2330", root=tmp_path)
    with patch("bot.fundamentals_refresh.build_fundamental_snapshot", return_value=snap):
        stats = run_refresh_queue(root=tmp_path, limit=1, delay_seconds=0)

    assert stats["succeeded"] == 1
    assert "2330" not in load_refresh_queue(root=tmp_path)
    assert (tmp_path / "data" / "fundamentals" / "2330" / "snapshot.json").exists()

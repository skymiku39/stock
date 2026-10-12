from __future__ import annotations

import importlib
import threading
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from bot.dashboard.task_operations import score_snapshot, to_data
from bot.dashboard.task_service import TaskService
from bot.ticker_view import TickerSnapshot
from tests.test_dashboard_tasks import wait_for

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "bot" / "dashboard" / "nav.py"


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    # No real data, config, broker connections or live sources in UI tests.
    monkeypatch.chdir(tmp_path)
    for module in ("common", "cache_helpers", "pages", "tasks", "workspace", "memory_page"):
        monkeypatch.setattr(importlib.import_module(f"bot.dashboard.{module}"), "PROJECT_ROOT", tmp_path)
    from bot.dashboard.cache_helpers import cached_env_values, cached_llm_today_stats
    cached_env_values.clear()
    cached_llm_today_stats.clear()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def execute(kind, params, root, progress):
        calls.append((kind, params))
        progress("離線測試正在分析", 0.5)
        entered.set()
        assert release.wait(15)
        if kind == "movers":
            return {"rows": [], "scanned": 0, "quoted": 0}
        snap = TickerSnapshot(ticker=params["tickers"][0], name="離線測試", fetched_at="2026-10-12T10:00:00")
        return [{"snapshot": to_data(snap), "scorecard": to_data(score_snapshot(snap))}]

    manager = TaskService(tmp_path, execute)
    monkeypatch.setattr("bot.dashboard.tasks.service", lambda: manager)
    monkeypatch.setattr("bot.dashboard.workspace.service", lambda: manager)
    try:
        yield manager, entered, release, calls
    finally:
        release.set()
        for job in manager.recent():
            if job["state"] in ("queued", "running"):
                wait_for(manager, job["id"])
        manager.close()
        cached_env_values.clear()
        cached_llm_today_stats.clear()


def assert_clean(at):
    assert not at.exception, [e.message for e in at.exception]


def test_workspace_navigation_and_release_notes(workspace):
    manager, _, _, calls = workspace
    at = AppTest.from_file(str(SCRIPT)).run(timeout=20)
    assert_clean(at)
    assert any("把注意力" in m.value for m in at.markdown)
    assert at.text_input(key="nav_search").placeholder == "搜尋頁面名稱…"
    at.button(key="nav_本次更新").click().run()
    assert_clean(at)
    assert at.title[0].value == "本次更新"
    assert any("哪些工作支援背景執行" in h.value for h in at.subheader)
    at.button(key="nav_任務中心").click().run()
    assert_clean(at)
    assert at.title[0].value == "任務中心"
    assert not calls
    assert manager.recent() == []


def test_analysis_survives_navigation_and_browser_session_recreation(workspace):
    manager, entered, release, calls = workspace
    at = AppTest.from_file(str(SCRIPT)).run(timeout=20)
    at.button(key="nav_個股深入分析").click().run()
    assert_clean(at)
    at.text_input(key="detail_symbol").set_value("2330").run()
    assert calls == []  # Editing a ticker does not launch research.
    at.button(key="detail_go").click().run(timeout=20)
    assert_clean(at)
    assert entered.wait(3)
    task_id = manager.recent()[0]["id"]
    at.button(key="nav_本次更新").click().run()
    assert manager.get(task_id)["state"] == "running"
    release.set()
    wait_for(manager, task_id)
    at.button(key="nav_個股深入分析").click().run(timeout=20)
    assert_clean(at)
    assert any("離線測試" in m.value for m in at.markdown)
    assert len(calls) == 1
    # A fresh browser session finds the persisted result without submitting again.
    reopened = AppTest.from_file(str(SCRIPT)).run(timeout=20)
    reopened.button(key="nav_個股深入分析").click().run(timeout=20)
    assert_clean(reopened)
    assert any("離線測試" in m.value for m in reopened.markdown)
    assert len(calls) == 1


def test_market_search_is_explicit_and_can_be_followed_from_task_center(workspace):
    manager, entered, release, calls = workspace
    at = AppTest.from_file(str(SCRIPT)).run(timeout=20)
    at.button(key="nav_盤中強弱排行").click().run(timeout=20)
    assert_clean(at)
    assert calls == []
    at.button(key="movers_search").click().run(timeout=20)
    assert entered.wait(3)
    at.button(key="task_center_quick").click().run()
    assert_clean(at)
    assert at.title[0].value == "任務中心"
    assert any("執行中" in m.value for m in at.markdown)
    release.set()
    job = wait_for(manager, manager.recent()[0]["id"])
    at.run()
    at.button(key=f"open_{job['id']}").click().run(timeout=20)
    assert_clean(at)
    assert len(calls) == 1


def test_research_pages_do_not_launch_tasks_or_fetch_when_visited(workspace):
    manager, _, _, calls = workspace
    at = AppTest.from_file(str(SCRIPT)).run(timeout=20)
    for page in ("LLM 法說分析", "自動化管線", "今日當沖戰情室", "明日當沖關注", "美股 / 跨市場", "目前持股分析"):
        at.button(key=f"nav_{page}").click().run(timeout=20)
        assert_clean(at)
    assert calls == []
    assert manager.recent() == []

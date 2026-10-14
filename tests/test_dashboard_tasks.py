from __future__ import annotations

import json
import threading
import time

import pytest

from bot.dashboard.task_service import TaskResult, TaskService


def wait_for(service, task_id, states=("succeeded", "partial", "failed")):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        task = service.get(task_id)
        if task["state"] in states:
            return task
        time.sleep(0.02)
    pytest.fail(f"Task did not reach {states}: {task}")


def test_jobs_survive_client_recreation_and_results_are_durable(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def execute(kind, params, root, progress):
        progress("正在搜尋公開資料", 0.4)
        entered.set()
        assert release.wait(5)
        return TaskResult({"ticker": params["ticker"]}, "完成")

    worker = TaskService(tmp_path, execute)
    reader = TaskService(tmp_path, start=False)
    try:
        task_id = worker.submit("test", "研究", "頁面", {"ticker": "2330"})
        assert entered.wait(4)
        assert reader.get(task_id)["state"] == "running"
        assert reader.get(task_id)["progress"] == 0.4
        # Another page/session submits the same request: only one execution.
        assert reader.submit("test", "研究", "頁面", {"ticker": "2330"}) == task_id
        release.set()
        done = wait_for(reader, task_id)
        assert done["result"]["data"] == {"ticker": "2330"}
        assert done["finished"] >= done["started"]
    finally:
        release.set()
        worker.close()
    reopened = TaskService(tmp_path, start=False)
    assert reopened.get(task_id)["result"] == done["result"]


def test_queue_is_serial_and_cancellation_never_cancels_running_job(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def execute(kind, params, root, progress):
        calls.append(params["n"])
        entered.set()
        assert release.wait(5)
        return params

    worker = TaskService(tmp_path, execute)
    try:
        first = worker.submit("test", "一", "頁面", {"n": 1})
        assert entered.wait(4)
        second = worker.submit("test", "二", "頁面", {"n": 2})
        other = TaskService(tmp_path, start=False)
        assert other._claim() is None
        assert other.get(second)["state"] == "queued"
        assert not other.cancel_queued(first)
        assert other.cancel_queued(second)
        release.set()
        wait_for(worker, first)
        assert calls == [1]
        assert worker.get(second)["state"] == "cancelled"
    finally:
        release.set()
        worker.close()


def test_failed_and_partial_jobs_are_not_reported_as_success(tmp_path):
    def execute(kind, params, root, progress):
        if params["fail"]:
            raise ValueError("來源暫時無法使用")
        return TaskResult({"ok": 1}, "一檔成功，一檔缺資料", ["2317 缺資料"])

    worker = TaskService(tmp_path, execute)
    try:
        failed = worker.submit("test", "失敗", "頁面", {"fail": True})
        row = wait_for(worker, failed)
        assert row["state"] == "failed"
        assert "來源暫時無法使用" in row["error"]
        assert row["result"] is None
        partial = worker.submit("test", "部分成功", "頁面", {"fail": False})
        row = wait_for(worker, partial)
        assert row["state"] == "partial"
        assert row["result"]["warnings"] == ["2317 缺資料"]
    finally:
        worker.close()


def test_stale_running_jobs_become_interrupted_without_retry(tmp_path):
    service = TaskService(tmp_path, start=False)
    task_id = service.submit("test", "研究", "頁面", {})
    service._claim()
    with service._connect() as db:
        db.execute("UPDATE tasks SET heartbeat=? WHERE id=?", (time.time() - 100, task_id))
    reopened = TaskService(tmp_path, start=False)
    assert reopened.get(task_id)["state"] == "interrupted"
    assert reopened._claim() is None
    assert reopened.submit("test", "研究", "頁面", {}) != task_id


def test_result_restores_nested_snapshot_schema_without_executing_data():
    from bot.dashboard.task_operations import restore_cards, score_snapshot, to_data
    from bot.ticker_view import TickerSnapshot
    from bot.technicals import TechnicalSnapshot
    from bot.fundamentals_fetcher import FundamentalSnapshot

    snapshot = TickerSnapshot(ticker="2330", fundamentals=FundamentalSnapshot(ticker="2330"),
                              technicals=TechnicalSnapshot(ticker="2330"))
    card = score_snapshot(snapshot)
    encoded = json.loads(json.dumps([{"snapshot": to_data(snapshot), "scorecard": to_data(card)}]))
    snap2, card2 = restore_cards(encoded)[0]
    assert snap2 == snapshot
    assert card2 == card
    assert snap2.fundamentals.revenue_yoy_streak() == snapshot.fundamentals.revenue_yoy_streak()


def test_ticker_task_defaults_remain_local_only(monkeypatch, tmp_path):
    from bot.dashboard.task_operations import execute_task
    from bot.ticker_view import TickerSnapshot
    seen = []

    def snapshot(ticker, root, **kwargs):
        seen.append(kwargs)
        assert root == tmp_path
        return TickerSnapshot(ticker=ticker)

    monkeypatch.setattr("bot.ticker_view.build_snapshot", snapshot)
    execute_task("ticker", {"tickers": ["2330"], "options": {"auto_fill_missing": False, "auto_llm": False}}, tmp_path, lambda *a: None)
    assert seen[0]["auto_fill_missing"] is False
    assert seen[0]["auto_llm"] is False
    assert seen[0]["macro_cache_only"] is True


def test_ticker_job_persists_numpy_values_from_technical_calculations(monkeypatch, tmp_path):
    import numpy as np
    from bot.dashboard.task_operations import execute_task, restore_cards
    from bot.ticker_view import TickerSnapshot
    from bot.technicals import TechnicalSnapshot

    snapshot = TickerSnapshot(
        ticker="2330", price=np.float64(100), volume=np.int64(1200),
        technicals=TechnicalSnapshot(ticker="2330", candle_bias=np.bool_(True)),
    )
    monkeypatch.setattr("bot.ticker_view.build_snapshot", lambda *a, **k: snapshot)
    worker = TaskService(tmp_path, execute_task)
    try:
        task_id = worker.submit("ticker", "分析", "個股深入分析", {"tickers": ["2330"]})
        task = wait_for(worker, task_id)
        assert task["state"] == "succeeded", task["error"]
        restored, _ = restore_cards(task["result"]["data"])[0]
        assert restored.technicals.candle_bias is True
        assert restored.price == 100
        assert restored.volume == 1200
    finally:
        worker.close()


def test_missing_numeric_values_are_saved_as_null():
    import numpy as np
    from bot.dashboard.task_operations import to_data

    data = to_data({"values": np.array([1.0, np.nan, np.inf]), "missing": float("-inf")})
    assert json.loads(json.dumps(data, allow_nan=False)) == {
        "values": [1.0, None, None], "missing": None,
    }


def test_supervisor_accepts_result_from_reloaded_module(tmp_path):
    from dataclasses import make_dataclass
    from types import FunctionType

    old_result = make_dataclass("TaskResult", ["data"])
    old_globals = {**TaskService._execute.__globals__, "TaskResult": old_result}
    stale_execute = FunctionType(TaskService._execute.__code__, old_globals)
    service = TaskService(tmp_path, lambda *args: TaskResult({"price": 2540}), start=False)
    task_id = service.submit("ticker", "分析", "個股深入分析", {})
    task = service._claim()
    service._finish(task, *stale_execute(service, task))
    row = service.get(task_id)
    assert row["state"] == "succeeded", row["error"]
    assert row["result"]["data"] == {"price": 2540}


def test_research_partial_failures_keep_successful_results(monkeypatch, tmp_path):
    from bot.dashboard.task_operations import execute_task

    def research(ticker, **kwargs):
        return {"sentiment": "neutral"} if ticker == "2330" else None

    monkeypatch.setattr("bot.auto_llm.auto_research_ticker", research)
    result = execute_task("research", {"tickers": ["2330", "2317"]}, tmp_path, lambda *a: None)
    assert result.data[0]["ticker"] == "2330"
    assert result.data[1]["error"]
    assert len(result.warnings) == 1
    with pytest.raises(RuntimeError, match="素材不足"):
        execute_task("research", {"tickers": ["2317"]}, tmp_path, lambda *a: None)


def test_cached_market_labels_never_restore_cloud_or_fetch(monkeypatch, tmp_path):
    from bot.market_meta import detect_market
    monkeypatch.setattr("bot.market_meta.restore_file_from_cloud", lambda *a, **k: pytest.fail("cloud restore"))
    assert detect_market("2330", root=tmp_path, cache_only=True) == "unknown"
    path = tmp_path / "data" / "meta" / "market_map.json"
    path.write_text(json.dumps({"date": "2020-01-01", "map": {"2330": "twse"}}), encoding="utf-8")
    assert detect_market("2330", root=tmp_path, cache_only=True) == "twse"


def test_saved_results_remain_discoverable_beyond_recent_list(tmp_path):
    service = TaskService(tmp_path, start=False)
    old = service.submit("ticker", "2330 個股分析", "個股深入分析", {"tickers": ["2330"]})
    with service._connect() as db:
        db.execute("UPDATE tasks SET state='succeeded' WHERE id=?", (old,))
    for index in range(55):
        other = service.submit("ticker", f"研究 {index}", "個股深入分析", {"tickers": [str(index)]})
        with service._connect() as db:
            db.execute("UPDATE tasks SET state='succeeded' WHERE id=?", (other,))
    assert old not in [job["id"] for job in service.recent()]
    assert service.latest("ticker", {"tickers": ["2330"]})["id"] == old
    assert service.recent(query="2330")[0]["id"] == old

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from pydantic import ValidationError

from bot.config import Settings
from bot.dashboard.task_service import TaskService
from bot.job_process import ProcessTerminationError, terminate_process_tree


def wait_until(check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("Worker did not reach expected state before the test deadline")


def finished(service, task_id):
    row = service.get(task_id)
    return row if row["finished"] is not None else None


def success_command(result_path, *, pause=0):
    payload = json.dumps(["succeeded", json.dumps({"data": {"ok": True}, "warnings": []}), "saved", ""])
    return [sys.executable, "-c", "from pathlib import Path; import time; "
            f"Path({str(result_path)!r}).write_text({payload!r}, encoding='utf-8'); time.sleep({pause})"]


def test_timeout_kills_worker_tree_before_next_job(tmp_path, monkeypatch):
    ready, late = tmp_path / "ready", tmp_path / "late"
    child = f"import time; from pathlib import Path; time.sleep(3); Path({str(late)!r}).touch()"
    hung = ("import subprocess, sys, time; from pathlib import Path; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            f"Path({str(ready)!r}).touch(); time.sleep(60)")
    processes = []
    original = subprocess.Popen

    def popen(cmd, **kwargs):
        proc = original(cmd, **kwargs)
        if cmd[0] == sys.executable:
            processes.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    service = TaskService(tmp_path, timeout_seconds=1.5)
    monkeypatch.setattr(service, "_worker_command", lambda task, output:
                        [sys.executable, "-c", hung] if task["params"]["hung"] else success_command(output))
    try:
        first = service.submit("test", "hung", "test", {"hung": True})
        wait_until(ready.exists)
        second = service.submit("test", "next", "test", {"hung": False})
        timed_out = wait_until(lambda: finished(service, first))
        assert timed_out["state"] == "timed_out"
        assert timed_out["result"] is None and timed_out["error"]
        assert processes[0].poll() is not None
        done = wait_until(lambda: finished(service, second))
        assert done["state"] == "succeeded"
        assert done["started"] >= timed_out["finished"]
        assert done["result"]["data"] == {"ok": True}
        time.sleep(2)
        assert not late.exists(), "A descendant continued writing after timeout"
        assert len(service.recent()) == 2  # No automatic retry of the first job.
    finally:
        service.close()


def test_close_terminates_running_worker_and_preserves_queue(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    service = TaskService(tmp_path, timeout_seconds=60)
    command = [sys.executable, "-c", "import time; from pathlib import Path; "
               f"Path({str(ready)!r}).touch(); time.sleep(60)"]
    monkeypatch.setattr(service, "_worker_command", lambda *args: command)
    try:
        first = service.submit("test", "first", "test", {"n": 1})
        wait_until(ready.exists)
        second = service.submit("test", "second", "test", {"n": 2})
    finally:
        service.close()
    assert not service._thread.is_alive()
    assert service.get(first)["state"] == "interrupted"
    assert service.get(second)["state"] == "queued"


def test_terminal_result_is_not_published_until_worker_exits(tmp_path, monkeypatch):
    service = TaskService(tmp_path, timeout_seconds=5)
    output_paths = []

    def command(task, output):
        output_paths.append(output)
        return success_command(output, pause=1)

    monkeypatch.setattr(service, "_worker_command", command)
    try:
        task_id = service.submit("test", "test", "test", {})
        wait_until(lambda: output_paths and output_paths[0].exists())
        assert service.get(task_id)["state"] == "running"
        assert wait_until(lambda: finished(service, task_id))["state"] == "succeeded"
        assert not output_paths[0].exists()
    finally:
        service.close()


def test_real_worker_persists_results_and_exceptions(tmp_path):
    # An empty ticker list exercises the real dispatcher without fetching data.
    service = TaskService(tmp_path, timeout_seconds=15)
    try:
        task_id = service.submit("ticker", "empty", "test", {"tickers": []})
        row = wait_until(lambda: finished(service, task_id))
        assert row["state"] == "succeeded", row["error"]
        assert row["result"]["data"] == []
        invalid = service.submit("unknown-audit-operation", "invalid", "test", {})
        row = wait_until(lambda: finished(service, invalid))
        assert row["state"] == "failed" and row["error"]
        assert row["result"] is None
    finally:
        service.close()
    assert TaskService(tmp_path, start=False).get(task_id)["result"]["data"] == []


def test_failed_termination_blocks_queue_across_restart(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise ProcessTerminationError("Cannot confirm termination of PID 123")
    monkeypatch.setattr("bot.job_process.run_with_deadline", fail)
    service = TaskService(tmp_path)
    try:
        first = service.submit("test", "first", "test", {"n": 1})
        row = wait_until(lambda: finished(service, first))
        assert row["state"] == "termination_failed"
        assert "123" in row["error"]
    finally:
        service.close()
    reopened = TaskService(tmp_path, start=False)
    second = reopened.submit("test", "second", "test", {"n": 2})
    assert reopened._claim() is None
    assert reopened.get(second)["state"] == "queued"
    assert reopened.submit("test", "first", "test", {"n": 1}) == first


def test_worker_parent_pipe_eof_terminates_descendants(tmp_path):
    ready, late = tmp_path / "ready", tmp_path / "late"
    child = f"import time; from pathlib import Path; time.sleep(2); Path({str(late)!r}).touch()"
    script = ("import subprocess, sys, threading, time; from pathlib import Path; "
              "from bot.dashboard.task_worker import _watch_parent; "
              "threading.Thread(target=_watch_parent, daemon=True).start(); "
              f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
              f"Path({str(ready)!r}).touch(); time.sleep(60)")
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, **options)
    try:
        wait_until(ready.exists)
        proc.stdin.close()  # The same EOF is delivered when the owner crashes.
        proc.wait(timeout=5)
        time.sleep(2.2)
        assert not late.exists()
    finally:
        if proc.poll() is None:
            terminate_process_tree(proc)
        if not proc.stdin.closed:
            proc.stdin.close()


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_dashboard_deadline_is_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        TaskService(tmp_path, start=False, timeout_seconds=value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, dashboard_task_timeout_seconds=value)


def test_dashboard_deadline_uses_project_env(tmp_path):
    (tmp_path / ".env").write_text("DASHBOARD_TASK_TIMEOUT_SECONDS=42\n", encoding="utf-8")
    assert TaskService(tmp_path, start=False).timeout_seconds == 42

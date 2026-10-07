from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from bot.config import Settings
from bot.job_process import run_with_deadline
from bot.scheduler import Scheduler


def scheduler(tmp_path, **kwargs):
    return Scheduler(Settings(_env_file=None, symbols=["2330"], scheduler_job_timeout_seconds=1),
                     project_root=tmp_path, **kwargs)


def status(sch):
    return json.loads(sch.status_path.read_text(encoding="utf-8"))


def test_hung_job_times_out_and_releases_lock(tmp_path, monkeypatch):
    sch = scheduler(tmp_path)
    job = sch.jobs[0]
    monkeypatch.setattr("bot.scheduler._resolve_cmd", lambda *args: [sys.executable, "-c", "import time; time.sleep(60)"])
    sch._run_job_with_lock(job, dt.datetime(2026, 10, 7, 10))
    result = status(sch)["jobs"][job.name]
    assert result["state"] == "timed_out"
    assert result["exit_code"] == 124
    assert result["started_at"] and result["finished_at"]
    assert "timeout" in result["error"]
    assert job._lock.acquire(blocking=False)
    job._lock.release()
    monkeypatch.setattr("bot.scheduler._resolve_cmd", lambda *args: [sys.executable, "-c", "print('recovered')"])
    assert sch._run_job(job) == 0
    assert status(sch)["jobs"][job.name]["state"] == "succeeded"


def test_job_status_survives_concurrent_completions_and_heartbeat(tmp_path):
    sch = scheduler(tmp_path)
    barrier = threading.Barrier(3)
    def complete(name, succeeded):
        barrier.wait()
        sch._write_status("running", last_job=name, last_success=succeeded,
                          job_state="succeeded" if succeeded else "failed", job_exit_code=0 if succeeded else 1)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(complete, "one", True), pool.submit(complete, "two", False)]
        barrier.wait()
        for future in futures:
            future.result()
    sch._write_status("running")
    assert status(sch)["jobs"]["one"]["state"] == "succeeded"
    assert status(sch)["jobs"]["two"]["state"] == "failed"


def test_dry_run_and_restart_do_not_invent_success(tmp_path):
    sch = scheduler(tmp_path, dry_run=True)
    sch._run_job(sch.jobs[0])
    assert status(sch)["jobs"][sch.jobs[0].name]["state"] == "dry_run"
    assert "last_success_at" not in status(sch)["jobs"][sch.jobs[0].name]
    sch._write_status("running", last_job="unfinished", job_state="running")
    restarted = scheduler(tmp_path)
    restarted._write_status("starting")
    assert status(restarted)["jobs"]["unfinished"]["state"] == "interrupted"


def test_failed_job_keeps_last_success_timestamp(tmp_path, monkeypatch):
    sch = scheduler(tmp_path)
    monkeypatch.setattr("bot.scheduler.run_with_deadline", lambda *args, **kwargs: 0)
    sch._run_job(sch.jobs[0])
    successful_at = status(sch)["jobs"][sch.jobs[0].name]["last_success_at"]
    monkeypatch.setattr("bot.scheduler.run_with_deadline", lambda *args, **kwargs: 7)
    assert sch._run_job(sch.jobs[0]) == 7
    entry = status(sch)["jobs"][sch.jobs[0].name]
    assert entry["state"] == "failed" and entry["exit_code"] == 7
    assert entry["last_success_at"] == successful_at


@pytest.mark.parametrize("value", [0, -1])
def test_unbounded_job_timeout_is_rejected(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, scheduler_job_timeout_seconds=value)


def test_timeout_reaps_its_child_process(tmp_path, monkeypatch):
    actual_popen = subprocess.Popen
    processes = []
    def record(*args, **kwargs):
        child = actual_popen(*args, **kwargs)
        processes.append(child)
        return child
    monkeypatch.setattr(subprocess, "Popen", record)
    with (tmp_path / "job.log").open("w") as log:
        with pytest.raises(subprocess.TimeoutExpired):
            run_with_deadline([sys.executable, "-c", "import time; time.sleep(60)"],
                              cwd=tmp_path, env=None, stdout=log, timeout=0.2)
    assert processes[0].poll() is not None


def test_timeout_terminates_spawned_descendants(tmp_path):
    ready, late = tmp_path / "ready", tmp_path / "late"
    grandchild = f"import time; from pathlib import Path; time.sleep(2); Path({str(late)!r}).touch()"
    parent = ("import subprocess, sys, time; from pathlib import Path; "
              f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
              f"Path({str(ready)!r}).touch(); time.sleep(60)")
    with (tmp_path / "tree.log").open("w") as log:
        with pytest.raises(subprocess.TimeoutExpired):
            run_with_deadline([sys.executable, "-c", parent], cwd=tmp_path, env=None, stdout=log, timeout=1)
    assert ready.exists(), "parent must have spawned its child before the timeout"
    time.sleep(1.5)
    assert not late.exists(), "descendant must not continue writing after the timeout"

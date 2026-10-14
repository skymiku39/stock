"""Durable dashboard jobs, independent of Streamlit sessions and reruns.

SQLite arbitrates one running job per project, including across dashboard
processes. A short lease detects a stopped server; interrupted jobs are never
retried automatically because they may have called a paid provider already.
Workers never receive Streamlit objects or session state.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

ACTIVE = ("queued", "running")
LEASE_SECONDS = 30


@dataclass
class TaskResult:
    data: Any
    message: str = "結果已保存"
    warnings: list[str] = field(default_factory=list)


class TaskService:
    def __init__(self, root: Path, execute: Callable | None = None, *, start: bool = True,
                 timeout_seconds: float | None = None):
        self.root = Path(root).resolve()
        if timeout_seconds is None:
            from bot.config import Settings
            timeout_seconds = Settings(_env_file=str(self.root / ".env")).dashboard_task_timeout_seconds
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Dashboard task timeout must be positive and finite")
        self.timeout_seconds = timeout_seconds
        self.path = self.root / "data" / "dashboard_tasks.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.owner = uuid.uuid4().hex
        # In-process execution is a test hook only. Production always uses the
        # bounded worker process so timed-out code cannot continue writing.
        self.execute = execute
        self._stop = threading.Event()
        self._wake = threading.Event()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    page TEXT NOT NULL, params TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                    progress REAL, created REAL NOT NULL, started REAL, finished REAL,
                    heartbeat REAL, owner TEXT, result TEXT, error TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_request ON tasks(fingerprint)
                    WHERE state IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS recent_tasks ON tasks(created DESC);
            """)
        self._thread = threading.Thread(target=self._loop, name="dashboard-tasks", daemon=True)
        if start:
            self._thread.start()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _recover(db, now: float):
        db.execute(
            "UPDATE tasks SET state='interrupted', finished=?, "
            "message='服務已中斷，請確認結果後手動重試', error='背景執行心跳已中斷' "
            "WHERE state='running' AND heartbeat < ?",
            (now, now - LEASE_SECONDS),
        )

    def submit(self, kind: str, title: str, page: str, params: dict) -> str:
        payload = json.dumps(params, ensure_ascii=False, sort_keys=True, allow_nan=False)
        fingerprint = hashlib.sha256(f"{kind}:{payload}".encode()).hexdigest()
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._recover(db, now)
            existing = db.execute(
                "SELECT id FROM tasks WHERE fingerprint=? AND state IN ('queued','running','termination_failed')",
                (fingerprint,),
            ).fetchone()
            if existing:
                return existing["id"]
            if db.execute("SELECT count(*) FROM tasks WHERE state IN ('queued','running','termination_failed')").fetchone()[0] >= 20:
                raise ValueError("目前已有 20 個待處理任務，請等候完成再送出。")
            task_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO tasks(id,kind,title,page,params,fingerprint,state,message,created) "
                "VALUES(?,?,?,?,?,?,'queued','等待前一個任務完成',?)",
                (task_id, kind, title, page, payload, fingerprint, now),
            )
        self._wake.set()
        return task_id

    @staticmethod
    def _record(row):
        if row is None:
            return None
        item = dict(row)
        item["params"] = json.loads(item["params"])
        item["result"] = json.loads(item["result"]) if item.get("result") else None
        return item

    def get(self, task_id: str):
        with self._connect() as db:
            self._recover(db, time.time())
            return self._record(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def recent(self, limit: int = 50, query: str = ""):
        with self._connect() as db:
            self._recover(db, time.time())
            # Do not load potentially large analysis results for status polling.
            rows = db.execute(
                "SELECT id,kind,title,page,params,state,message,progress,created,started,finished,error "
                "FROM tasks WHERE instr(title, ?) > 0 ORDER BY created DESC LIMIT ?", (query, limit),
            ).fetchall()
            return [self._record(row) for row in rows]

    def latest(self, kind: str, match: dict):
        """Find a saved result even after it falls outside the recent task list."""
        with self._connect() as db:
            self._recover(db, time.time())
            for row in db.execute("SELECT id,params FROM tasks WHERE kind=? ORDER BY created DESC", (kind,)):
                params = json.loads(row["params"])
                if all(params.get(key) == value for key, value in match.items()):
                    return self._record(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())
        return None

    def cancel_queued(self, task_id: str) -> bool:
        with self._connect() as db:
            return db.execute(
                "UPDATE tasks SET state='cancelled', message='已取消排隊', finished=? "
                "WHERE id=? AND state='queued'", (time.time(), task_id),
            ).rowcount == 1

    def _claim(self):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self._recover(db, now)
            if db.execute("SELECT 1 FROM tasks WHERE state IN ('running','termination_failed')").fetchone():
                return None
            row = db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE tasks SET state='running', started=?, heartbeat=?, owner=?, "
                "message='正在準備資料' WHERE id=?", (now, now, self.owner, row["id"]),
            )
            return self._record(row)

    def _progress(self, task_id: str, message: str, fraction: float | None = None):
        with self._connect() as db:
            db.execute(
                "UPDATE tasks SET message=?, progress=? WHERE id=? AND owner=? AND state='running'",
                (message, None if fraction is None else max(0, min(1, fraction)), task_id, self.owner),
            )

    def _run(self, task: dict):
        self._finish(task, *self._execute(task))

    def _execute(self, task: dict):
        try:
            execute = self.execute
            if execute is None:
                from bot.dashboard.task_operations import execute_task
                execute = execute_task
            result = execute(
                task["kind"], task["params"], self.root,
                lambda message, fraction=None: self._progress(task["id"], message, fraction),
            )
            # Streamlit may reload this module while an older supervisor lives.
            # The dispatcher then returns the newly loaded TaskResult class.
            from bot.dashboard.task_service import TaskResult as CurrentTaskResult
            if not isinstance(result, (TaskResult, CurrentTaskResult)):
                result = TaskResult(result)
            encoded = json.dumps(
                {"data": result.data, "warnings": result.warnings}, ensure_ascii=False, allow_nan=False,
            )
            state = "partial" if result.warnings else "succeeded"
            message, error = result.message, ""
        except Exception as exc:
            state, encoded = "failed", None
            message, error = "執行失敗，可查看原因後重試", f"{type(exc).__name__}: {exc}"
        return state, encoded, message, error

    def _finish(self, task, state, encoded, message, error):
        with self._connect() as db:
            db.execute(
                "UPDATE tasks SET state=?,result=?,message=?,error=?,finished=?,progress=? "
                "WHERE id=? AND owner=? AND state='running'",
                (state, encoded, message, error, time.time(), 1 if state in ('succeeded', 'partial') else None,
                 task["id"], self.owner),
            )

    def _worker_command(self, task, result_path):
        return [sys.executable, "-B", "-m", "bot.dashboard.task_worker",
                "--root", str(self.root), "--task-id", task["id"],
                "--owner", self.owner, "--result", str(result_path)]

    def _run_process(self, task):
        from bot.job_process import ProcessCancelled, ProcessTerminationError, run_with_deadline

        log_dir = self.root / "logs" / "dashboard_tasks"
        result_path = self.path.parent / f".task-result-{task['id']}-{self.owner}.json"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / f"{task['id']}.log").open("w", encoding="utf-8") as log:
                code = run_with_deadline(
                    self._worker_command(task, result_path), cwd=self.root,
                    env={**os.environ, "PYTHONUTF8": "1"}, stdout=log,
                    timeout=self.timeout_seconds, cancel_event=self._stop, guard_parent=True,
                )
            if code != 0:
                raise RuntimeError(f"Worker exited with code {code}; see logs/dashboard_tasks/{task['id']}.log")
            outcome = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(outcome, list) or len(outcome) != 4 or outcome[0] not in ("succeeded", "partial", "failed"):
                raise ValueError("Invalid worker result")
        except subprocess.TimeoutExpired:
            outcome = ("timed_out", None, "任務逾時，請確認結果後手動重試",
                       f"超過 {self.timeout_seconds:g} 秒執行上限；工作程序已終止，已提交資料未回滾")
        except ProcessCancelled:
            outcome = ("interrupted", None, "服務已停止，請確認結果後手動重試", "工作程序已終止，已提交資料未回滾")
        except ProcessTerminationError as exc:
            outcome = ("termination_failed", None, "無法確認工作程序已終止，佇列暫停，請人工處理", str(exc))
        except Exception as exc:
            outcome = ("failed", None, "執行失敗，可查看原因後重試", f"{type(exc).__name__}: {exc}")
        finally:
            try:
                result_path.unlink(missing_ok=True)
            except OSError:
                pass  # A leftover result file must not hide the terminal state.
        self._finish(task, *outcome)

    def _loop(self):
        worker = None
        task = None
        while not self._stop.is_set():
            try:
                if worker is not None and worker.is_alive():
                    with self._connect() as db:
                        updated = db.execute(
                            "UPDATE tasks SET heartbeat=? WHERE id=? AND owner=? AND state='running'",
                            (time.time(), task["id"], self.owner),
                        ).rowcount
                    if not updated:
                        # Another owner recovered the lease. Stop our child
                        # instead of letting a stale worker keep mutating data.
                        current = self.get(task["id"])
                        if current is None or current["owner"] != self.owner or current["state"] == "interrupted":
                            self._stop.set()
                else:
                    task = self._claim()
                    if task:
                        target = self._run if self.execute is not None else self._run_process
                        worker = threading.Thread(target=target, args=(task,), daemon=True)
                        worker.start()
            except sqlite3.Error:
                # A transient lock must not silently kill the queue supervisor.
                pass
            self._wake.wait(1)
            self._wake.clear()
        if worker is not None:
            worker.join(timeout=20 if self.execute is None else 3)

    def close(self):
        """Stop owned work before releasing the queue; never retry it automatically."""
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=22)


@lru_cache(maxsize=None)
def get_task_service(root: str) -> TaskService:
    return TaskService(Path(root))

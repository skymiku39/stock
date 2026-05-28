"""process_runner -- 從儀表板啟動/停止 stock-bot 子行程的最小封裝。

* 將子行程 stdout/stderr 寫入 log/dashboard_run_<timestamp>.log
* 維護單例狀態，避免重複啟動
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class RunRecord:
    pid: int
    run_mode: str
    log_path: Path
    started_at: float
    ended_at: Optional[float] = None
    return_code: Optional[int] = None
    extra_env: Dict[str, str] = field(default_factory=dict)


class BotProcessRunner:
    """單例 stock-bot 子行程管理。"""

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = project_root or Path.cwd()
        self.log_dir = self.project_root / "log"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._proc: Optional[subprocess.Popen] = None
        self._record: Optional[RunRecord] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 狀態
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        with self._lock:
            if self._proc is None:
                return False
            return self._proc.poll() is None

    def current(self) -> Optional[RunRecord]:
        with self._lock:
            if self._proc is None:
                return self._record
            rc = self._proc.poll()
            if rc is not None and self._record is not None and self._record.return_code is None:
                self._record.return_code = rc
                self._record.ended_at = time.time()
            return self._record

    # ------------------------------------------------------------------
    # 啟動 / 停止
    # ------------------------------------------------------------------

    def start(
        self,
        run_mode: str,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> RunRecord:
        if self.is_running():
            assert self._record is not None
            return self._record

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = self.log_dir / f"dashboard_run_{run_mode}_{timestamp}.log"

        env = os.environ.copy()
        env["RUN_MODE"] = run_mode
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if extra_env:
            env.update(extra_env)

        cmd = self._build_command()

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        log_fp = open(log_path, "w", encoding="utf-8", buffering=1)
        log_fp.write(
            f"# stock-bot dashboard run\n"
            f"# command: {' '.join(cmd)}\n"
            f"# run_mode: {run_mode}\n"
            f"# started_at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# cwd: {self.project_root}\n"
            f"# ---\n"
        )
        log_fp.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_root),
            env=env,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

        record = RunRecord(
            pid=proc.pid,
            run_mode=run_mode,
            log_path=log_path,
            started_at=time.time(),
            extra_env=dict(extra_env or {}),
        )
        with self._lock:
            self._proc = proc
            self._record = record

        threading.Thread(
            target=self._wait_and_close,
            args=(proc, log_fp, record),
            daemon=True,
        ).start()

        return record

    def _wait_and_close(
        self,
        proc: subprocess.Popen,
        log_fp,
        record: RunRecord,
    ) -> None:
        rc = proc.wait()
        try:
            log_fp.write(f"\n# ---\n# exit_code: {rc}\n")
            log_fp.flush()
            log_fp.close()
        except Exception:
            pass
        record.return_code = rc
        record.ended_at = time.time()

    def stop(self, timeout: float = 10.0) -> bool:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return True

        try:
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------
    # 內部
    # ------------------------------------------------------------------

    def _build_command(self) -> List[str]:
        """優先用 uv run，其次 fallback 到當前 Python -m bot.main。"""
        from shutil import which

        uv = which("uv")
        if uv:
            return [uv, "run", "stock-bot"]
        return [sys.executable, "-m", "bot.main"]


# ----------------------------------------------------------------------
# 全域單例 (供 Streamlit session 共用)
# ----------------------------------------------------------------------

_runner: Optional[BotProcessRunner] = None


def get_runner(project_root: Optional[Path] = None) -> BotProcessRunner:
    global _runner
    if _runner is None:
        _runner = BotProcessRunner(project_root=project_root)
    return _runner


def tail_file(path: Path, lines: int = 200) -> str:
    """讀取檔案最後 N 行 (簡單版，適合 <= MB 級 log)。"""
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
        return "".join(data[-lines:])
    except Exception as e:
        return f"<read error: {e}>"


__all__ = ["BotProcessRunner", "RunRecord", "get_runner", "tail_file"]

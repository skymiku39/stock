"""stock-scheduler -- 常駐背景排程器。

讓「股票資訊持續自動更新 + 盤中監測」不必開著儀表板也能跑：

  * macro      : 輕量行情 / 總經刷新 (呼叫 stock-macro-update，不打 LLM)
  * research   : 完整研究管線 (呼叫 stock-auto-research，含 ETF/籌碼/基本面/LLM 簡報)
  * company    : 公司基本資料補齊 (stock-company-update)
  * cloud_sync     : Google Sheets 同步 (stock-cloud-sync，含 LLM 分析表)
  * history_fetch  : 慢速補齊歷史日 K (stock-history-fetch，避免 TWSE 403)
  * monitor        : 盤中自動托管 stock-bot 監測子行程 (開盤啟動、收盤停止)

各任務的間隔、是否只在交易時段執行，皆由 .env 的 SCHEDULER_* 設定控制。

用法:
    uv run stock-scheduler            # 常駐執行 (Ctrl+C 結束)
    uv run stock-scheduler --once     # 把目前到期的任務各跑一次就結束 (適合排程器/工作排程器)
    uv run stock-scheduler --dry-run  # 只印出排程計畫，不實際執行
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from bot.config import Settings
from bot.events import SchedulerJobCompleted, SchedulerStarted
from bot.events.protocols import EventPublisher
from bot.events.wiring import publish_if_bus
from bot.job_process import run_with_deadline
from bot.utils import get_logger, now_tw

# 台股交易時段 (含盤前/盤後緩衝)，用於 market_hours_only 判斷。
_DATA_WINDOW_START = dt.time(8, 30)
_DATA_WINDOW_END = dt.time(14, 30)
# 盤中監測子行程的開/收盤時間。
_MONITOR_OPEN = dt.time(9, 0)
_MONITOR_CLOSE = dt.time(13, 35)


def _is_weekday(d: dt.date) -> bool:
    return d.weekday() < 5  # 0=Mon ... 4=Fri


def _in_window(t: dt.time, start: dt.time, end: dt.time) -> bool:
    return start <= t <= end


def _base_command() -> List[str]:
    """優先用 uv run，否則 fallback 到當前 Python -m。"""
    return [sys.executable, "-m"]


def _resolve_cmd(console_script: str, module: str, extra: List[str]) -> List[str]:
    base = _base_command()
    if base[-1] == "run":  # uv run <console-script>
        return base + [console_script] + extra
    return base + [module] + extra  # python -m <module>


class _ProcessLock:
    """Hold a non-blocking OS file lock for this scheduler process lifetime."""

    def __init__(self, project_root: Path):
        self.path = project_root / "data" / ".scheduler.lock"
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


@dataclass
class Job:
    name: str
    console_script: str
    module: str
    interval_min: int
    market_hours_only: bool
    extra_args: List[str] = field(default_factory=list)
    run_once_per_day: bool = False
    window_start: Optional[dt.time] = None
    window_end: Optional[dt.time] = None
    report_kind: str = ""
    report_mode: str = ""
    target_date_mode: str = ""
    last_run_epoch: float = 0.0
    last_run_key: str = ""
    consecutive_failures: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_running(self) -> bool:
        locked = self._lock.acquire(blocking=False)
        if locked:
            self._lock.release()
            return False
        return True


_DAILY_FAILURE_RETRY_SECONDS = 5 * 60


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        *,
        project_root: Optional[Path] = None,
        dry_run: bool = False,
        publisher: Optional[EventPublisher] = None,
    ):
        self.settings = settings
        self.project_root = project_root or Path.cwd()
        self.dry_run = dry_run
        self._publisher = publisher
        self.logger = get_logger("scheduler", log_dir=self.project_root / "log")
        self.log_dir = self.project_root / "log"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.status_path = self.project_root / "data" / "scheduler_status.json"
        self._stop = threading.Event()
        self._status_lock = threading.RLock()
        self.jobs: List[Job] = self._build_jobs()
        self._restore_job_state()
        self._monitor_runner = None  # 延遲建立，避免無謂 import

    def _restore_job_state(self) -> None:
        try:
            jobs = json.loads(self.status_path.read_text(encoding="utf-8")).get("jobs", {})
        except (OSError, ValueError):
            return
        for job in self.jobs:
            prior = jobs.get(job.name, {})
            if prior.get("state") not in {"succeeded", "failed", "timed_out"}:
                continue
            try:
                job.last_run_epoch = dt.datetime.fromisoformat(prior["finished_at"]).timestamp()
                job.consecutive_failures = int(prior.get("consecutive_failures", 0))
            except (KeyError, ValueError, TypeError):
                continue

    def _write_status(self, state: str, **kwargs) -> None:
        # Heartbeat and concurrent job completions must not lose each other's updates.
        with self._status_lock:
            self._write_status_unlocked(state, **kwargs)

    def _write_status_unlocked(
        self,
        state: str,
        *,
        run_once: Optional[bool] = None,
        last_job: Optional[str] = None,
        last_success: Optional[bool] = None,
        last_error: Optional[str] = None,
        job_state: Optional[str] = None,
        job_exit_code: Optional[int] = None,
        job_log_path: Optional[str] = None,
    ) -> None:
        """Publish a durable heartbeat for the dashboard and diagnostics."""
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            previous: dict = {}
            if self.status_path.exists():
                try:
                    previous = json.loads(self.status_path.read_text(encoding="utf-8"))
                except Exception:
                    previous = {}
            heartbeat = now_tw().isoformat(timespec="seconds")
            payload = {
                **previous,
                "state": state,
                "pid": os.getpid(),
                "heartbeat_at": heartbeat,
                "run_once": run_once if run_once is not None else previous.get("run_once", False),
            }
            if last_job is not None:
                payload["last_job"] = last_job
                payload["last_job_at"] = heartbeat
            if last_success is not None:
                payload["last_success"] = last_success
            if last_error is not None:
                payload["last_error"] = last_error
            jobs = dict(previous.get("jobs") or {})
            if state == "starting":
                for name, prior in jobs.items():
                    if prior.get("state") == "running":
                        jobs[name] = {**prior, "state": "interrupted", "finished_at": heartbeat,
                                      "error": "scheduler restarted before completion was recorded"}
            if last_job is not None and job_state is not None:
                entry = dict(jobs.get(last_job) or {})
                entry.update(state=job_state, error=last_error or "", exit_code=job_exit_code)
                if job_log_path is not None:
                    entry["log_path"] = job_log_path
                if job_state == "running":
                    entry.update(started_at=heartbeat, finished_at="")
                else:
                    entry["finished_at"] = heartbeat
                if job_state == "succeeded":
                    entry["last_success_at"] = heartbeat
                job = next((j for j in self.jobs if j.name == last_job), None)
                if job is not None:
                    entry["consecutive_failures"] = job.consecutive_failures
                jobs[last_job] = entry
            payload["jobs"] = jobs
            # Use a process/thread-specific temporary file.  A fixed
            # ``scheduler_status.tmp`` lets two scheduler instances race on
            # Windows, where one process can replace/delete the other's open
            # temp file and produce WinError 32.  The target can also be read
            # by Streamlit at the same time, so retry the atomic replace
            # briefly before reporting a genuine write failure.
            tmp = self.status_path.with_name(
                f"{self.status_path.name}.{os.getpid()}."
                f"{threading.get_ident()}.tmp"
            )
            serialized = json.dumps(payload, ensure_ascii=False, indent=2)
            try:
                for attempt in range(4):
                    try:
                        tmp.write_text(serialized, encoding="utf-8")
                        tmp.replace(self.status_path)
                        return
                    except PermissionError:
                        if attempt >= 3:
                            raise
                        time.sleep(0.05 * (attempt + 1))
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            self.logger.debug("scheduler status 寫入失敗", exc_info=True)

    def _build_jobs(self) -> List[Job]:
        s = self.settings
        jobs: List[Job] = []
        if s.scheduler_macro_interval_min > 0:
            jobs.append(Job(
                name="macro",
                console_script="stock-macro-update",
                module="bot.macro_update",
                interval_min=s.scheduler_macro_interval_min,
                market_hours_only=s.scheduler_market_hours_only,
            ))
        if s.scheduler_fundamentals_interval_min > 0:
            extra = [a for a in str(s.scheduler_fundamentals_args).split() if a]
            jobs.append(Job(
                name="fundamentals",
                console_script="stock-fundamentals-refresh",
                module="bot.fundamentals_refresh",
                interval_min=s.scheduler_fundamentals_interval_min,
                market_hours_only=s.scheduler_market_hours_only,
                extra_args=extra,
            ))
        if s.scheduler_daily_all_enabled:
            jobs.append(Job(name="research", console_script="stock-daily-research",
                            module="bot.daily_research",
                            interval_min=s.scheduler_daily_all_interval_min,
                            market_hours_only=False))
        elif s.scheduler_research_interval_min > 0:
            extra = [a for a in str(s.scheduler_research_args).split() if a]
            if not any(
                arg == "--auto-research-limit"
                or arg.startswith("--auto-research-limit=")
                for arg in extra
            ):
                extra.extend([
                    "--auto-research-limit",
                    str(s.scheduler_research_max_tickers),
                ])
            jobs.append(Job(
                name="research",
                console_script="stock-auto-research",
                module="bot.auto_research",
                interval_min=s.scheduler_research_interval_min,
                market_hours_only=s.scheduler_market_hours_only,
                extra_args=extra,
            ))
        if getattr(s, "scheduler_intraday_enabled", False):
            jobs.append(Job(
                name="intraday",
                console_script="stock-intraday",
                module="bot.intraday_cli",
                interval_min=1440,
                market_hours_only=False,
                extra_args=["--refresh-news"],
                run_once_per_day=True,
                window_start=s.scheduler_intraday_time,
                window_end=s.scheduler_intraday_end_time,
                report_kind="intraday",
            ))
        if getattr(s, "scheduler_nextday_draft_enabled", False):
            jobs.append(Job(
                name="nextday_draft",
                console_script="stock-nextday",
                module="bot.next_day_watch_cli",
                interval_min=1440,
                market_hours_only=False,
                extra_args=["--mode", "draft", "--refresh-news"],
                run_once_per_day=True,
                window_start=s.scheduler_nextday_draft_time,
                window_end=s.scheduler_nextday_draft_end_time,
                report_kind="next_day_watch",
                report_mode="draft",
                target_date_mode="next_trading_day",
            ))
        if getattr(s, "scheduler_nextday_update_enabled", False):
            jobs.append(Job(
                name="nextday_update",
                console_script="stock-nextday",
                module="bot.next_day_watch_cli",
                interval_min=1440,
                market_hours_only=False,
                extra_args=["--mode", "update", "--refresh-macro"],
                run_once_per_day=True,
                window_start=s.scheduler_nextday_update_time,
                window_end=s.scheduler_nextday_update_end_time,
                report_kind="next_day_watch",
                report_mode="update",
                target_date_mode="today",
            ))
        if getattr(s, "scheduler_company_interval_min", 0) > 0:
            jobs.append(Job(
                name="company",
                console_script="stock-company-update",
                module="bot.company_update",
                interval_min=s.scheduler_company_interval_min,
                market_hours_only=False,  # 公司基本資料為靜態，任何時段皆可補
            ))
        if getattr(s, "scheduler_cloud_sync_interval_min", 0) > 0:
            jobs.append(Job(
                name="cloud_sync",
                console_script="stock-cloud-sync",
                module="bot.cloud_sync_cli",
                interval_min=s.scheduler_cloud_sync_interval_min,
                market_hours_only=False,
            ))
        if getattr(s, "scheduler_history_fetch_interval_min", 0) > 0:
            extra = [a for a in str(s.scheduler_history_fetch_args).split() if a]
            jobs.append(Job(
                name="history_fetch",
                console_script="stock-history-fetch",
                module="bot.history_fetch_cli",
                interval_min=s.scheduler_history_fetch_interval_min,
                market_hours_only=False,
                extra_args=extra,
                window_start=getattr(s, "scheduler_history_fetch_time", None),
                window_end=getattr(s, "scheduler_history_fetch_end_time", None),
            ))
        if getattr(s, "scheduler_watch_snapshot_interval_min", 0) > 0:
            jobs.append(Job(
                name="watch_snapshot",
                console_script="stock-watch-snapshot",
                module="bot.watch_snapshot",
                interval_min=s.scheduler_watch_snapshot_interval_min,
                market_hours_only=True,
            ))
        return jobs

    # ------------------------------------------------------------------
    # 任務執行
    # ------------------------------------------------------------------

    def _job_target_date(self, job: Job, now: dt.datetime) -> Optional[dt.date]:
        if job.target_date_mode == "today":
            return now.date()
        if job.target_date_mode == "next_trading_day":
            from bot.next_day_watch_pipeline import next_trading_day
            return next_trading_day(now.date())
        return None

    def _job_extra_args(self, job: Job, now: dt.datetime) -> List[str]:
        extra = list(job.extra_args)
        target_date = self._job_target_date(job, now)
        if target_date is not None:
            extra.extend(["--target-date", target_date.isoformat()])
        return extra

    def _daily_run_key(self, job: Job, now: dt.datetime) -> str:
        target_date = self._job_target_date(job, now)
        date_part = target_date.isoformat() if target_date else now.date().isoformat()
        mode_part = f":{job.report_mode}" if job.report_mode else ""
        return f"{job.name}:{date_part}{mode_part}"

    def _job_in_window(self, job: Job, now: dt.datetime) -> bool:
        if not _is_weekday(now.date()):
            return False
        if job.window_start is None:
            return True
        window_end = job.window_end or dt.time(23, 59)
        return _in_window(now.time(), job.window_start, window_end)

    def _job_eligible(self, job: Job, now: dt.datetime, *, once: bool) -> bool:
        """Apply the same due/window/idempotence rules to daemon and --once runs."""
        if job.is_running():
            return False
        if job.run_once_per_day:
            if not self._job_in_window(job, now):
                return False
            run_key = self._daily_run_key(job, now)
            if job.last_run_key == run_key:
                return False
            if (
                job.last_run_epoch > 0
                and time.time() - job.last_run_epoch < _DAILY_FAILURE_RETRY_SECONDS
            ):
                return False
            if self._report_exists_for_job(job, now):
                self.logger.info("skip %s; report already exists for %s", job.name, run_key)
                job.last_run_key = run_key
                job.last_run_epoch = time.time()
                return False
        else:
            retrying = 0 < job.consecutive_failures <= self.settings.scheduler_failure_max_retries
            delay = self.settings.scheduler_failure_retry_seconds if retrying else job.interval_min * 60
            if (not once or retrying) and (time.time() - job.last_run_epoch) < delay:
                return False
            if job.window_start is not None and not self._job_in_window(job, now):
                return False
        if job.market_hours_only and not _is_data_window(now):
            return False
        return True

    def _report_exists_for_job(self, job: Job, now: dt.datetime) -> bool:
        try:
            if job.report_kind == "intraday":
                from bot.intraday_pipeline import load_intraday_by_date
                data = load_intraday_by_date(self.project_root, now.date())
                return bool(data and (data.get("brief_md") or data.get("rankings")))
            if job.report_kind == "next_day_watch":
                from bot.next_day_watch_pipeline import load_next_day_by_date
                target_date = self._job_target_date(job, now)
                if target_date is None:
                    return False
                data = load_next_day_by_date(
                    self.project_root,
                    target_date,
                    mode=job.report_mode or None,
                    prefer_update=job.report_mode == "update",
                )
                return bool(data and (data.get("brief_md") or data.get("rankings")))
        except Exception:
            self.logger.debug("daily report existence check failed: %s", job.name, exc_info=True)
        return False

    def _run_job(self, job: Job, now: Optional[dt.datetime] = None) -> int:
        scheduled_at = now or now_tw()
        cmd = _resolve_cmd(job.console_script, job.module, self._job_extra_args(job, scheduled_at))
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_path = self.log_dir / f"scheduler_{job.name}_{ts}.log"
        self._write_status("running", last_job=job.name, job_state="running", job_log_path=str(log_path))
        self.logger.info("▶ 執行任務 %s: %s → %s", job.name, " ".join(cmd), log_path.name)
        rc = 0
        if self.dry_run:
            self.logger.info("  (dry-run，略過實際執行)")
            job.last_run_epoch = time.time()
            if job.run_once_per_day:
                job.last_run_key = self._daily_run_key(job, scheduled_at)
            publish_if_bus(
                self._publisher,
                SchedulerJobCompleted(
                    job_name=job.name,
                    success=True,
                    exit_code=0,
                    dry_run=True,
                    log_path=str(log_path),
                ),
            )
            self._write_status("running", last_job=job.name, last_error="", job_state="dry_run", job_exit_code=0)
            return 0

        if job.consecutive_failures > self.settings.scheduler_failure_max_retries:
            job.consecutive_failures = 0
        timeout = (self.settings.scheduler_research_timeout_seconds if job.name == "research"
                   else self.settings.scheduler_job_timeout_seconds)
        env = _child_env(self.project_root)
        error = ""
        job_state = "failed"
        try:
            with open(log_path, "w", encoding="utf-8", buffering=1) as fp:
                fp.write(f"# job={job.name} cmd={' '.join(cmd)} start={ts}\n# ---\n")
                fp.flush()
                rc = run_with_deadline(
                    cmd, cwd=str(self.project_root), env=env,
                    stdout=fp, timeout=timeout,
                )
                fp.write(f"\n# ---\n# exit_code: {rc}\n")
            level = self.logger.info if rc == 0 else self.logger.warning
            level("■ 任務 %s 結束 (exit=%s)", job.name, rc)
            job_state = "succeeded" if rc == 0 else "failed"
        except subprocess.TimeoutExpired:
            rc = 124
            job_state = "timed_out"
            error = f"timeout after {timeout}s"
            self.logger.warning("任務 %s 逾時：%s", job.name, error)
        except Exception as exc:
            self.logger.exception("任務 %s 執行失敗", job.name)
            rc = -1
            error = f"{type(exc).__name__}: {exc}"
        finally:
            job.consecutive_failures = job.consecutive_failures + 1 if rc != 0 else 0
            job.last_run_epoch = time.time()
            if job.run_once_per_day and rc == 0:
                job.last_run_key = self._daily_run_key(job, scheduled_at)
            publish_if_bus(
                self._publisher,
                SchedulerJobCompleted(
                    job_name=job.name,
                    success=rc == 0,
                    exit_code=rc,
                    dry_run=False,
                    log_path=str(log_path),
                ),
            )
            self._write_status(
                "running",
                last_job=job.name,
                last_success=rc == 0,
                last_error="" if rc == 0 else error or f"exit_code={rc}",
                job_state=job_state,
                job_exit_code=rc,
            )
        return rc

    def _dispatch(self, job: Job, now: dt.datetime) -> None:
        if not self._job_eligible(job, now, once=False):
            return

        threading.Thread(
            target=self._run_job_with_lock,
            args=(job, now),
            name=f"job-{job.name}",
            daemon=True,
        ).start()

    def _run_job_with_lock(self, job: Job, now: dt.datetime) -> None:
        if not job._lock.acquire(blocking=False):
            self.logger.debug("略過重複派送中的任務 %s", job.name)
            return
        try:
            self._run_job(job, now)
        finally:
            job._lock.release()

    # ------------------------------------------------------------------
    # 盤中監測子行程托管
    # ------------------------------------------------------------------

    def _resolve_monitor_mode(self) -> str:
        """盤中子行程模式；封存時禁止 trade。"""
        mode = self.settings.scheduler_monitor_mode
        archived = getattr(self.settings, "day_trading_archived", True)
        unfreeze = getattr(self.settings, "day_trading_unfreeze", False)
        if mode == "trade" and archived and not unfreeze:
            self.logger.warning(
                "當沖已封存：SCHEDULER_MONITOR_MODE=trade 已強制改為 watch（只看不買）",
            )
            return "watch"
        return mode

    def _supervise_monitor(self, now: dt.datetime) -> None:
        if not self.settings.scheduler_supervise_monitor:
            return
        if self._monitor_runner is None:
            from bot.process_runner import get_runner
            self._monitor_runner = get_runner(self.project_root)

        runner = self._monitor_runner
        monitor_mode = self._resolve_monitor_mode()
        should_run = _is_weekday(now.date()) and _in_window(
            now.time(), _MONITOR_OPEN, _MONITOR_CLOSE
        )
        running = runner.is_running()
        if should_run and not running:
            if self.dry_run:
                self.logger.info("  (dry-run) 應啟動監測子行程 (%s)", monitor_mode)
                return
            rec = runner.start(run_mode=monitor_mode)
            self.logger.info("盤中啟動監測子行程 PID=%s mode=%s", rec.pid, rec.run_mode)
        elif running and not should_run:
            if self.dry_run:
                self.logger.info("  (dry-run) 應停止監測子行程 (非交易時段)")
                return
            ok = runner.stop()
            self.logger.info("收盤停止監測子行程 (ok=%s)", ok)

    # ------------------------------------------------------------------
    # 主迴圈
    # ------------------------------------------------------------------

    def run(self, *, once: bool = False) -> int:
        self.logger.info("=== Scheduler 啟動 (once=%s, dry_run=%s) ===", once, self.dry_run)
        self._write_status("starting", run_once=once)
        publish_if_bus(
            self._publisher,
            SchedulerStarted(
                job_names=tuple(j.name for j in self.jobs),
                dry_run=self.dry_run,
                run_once=once,
            ),
        )
        self.logger.info(
            "任務: %s | tick=%ds | market_hours_only=%s | supervise_monitor=%s",
            [f"{j.name}({j.interval_min}m)" for j in self.jobs],
            self.settings.scheduler_tick_seconds,
            self.settings.scheduler_market_hours_only,
            self.settings.scheduler_supervise_monitor,
        )
        if not self.jobs and not self.settings.scheduler_supervise_monitor:
            self.logger.warning("沒有啟用任何任務 (檢查 SCHEDULER_* 設定)")
            self._write_status("stopped", run_once=once)
            return 0

        if once:
            now = now_tw()
            exit_code = 0
            for job in self.jobs:
                if not self._job_eligible(job, now, once=True):
                    self.logger.info("略過 %s (尚未到窗口、非交易日、未到期或已有報告)", job.name)
                    continue
                rc = self._run_job(job, now)
                if rc != 0 and exit_code == 0:
                    exit_code = rc
            self._supervise_monitor(now)
            self._write_status("stopped", run_once=True)
            self.logger.info("=== Scheduler --once 完成 ===")
            return exit_code

        # 啟動即跑一輪 (不受 market_hours_only 限制，方便即時看到資料)
        if self.settings.scheduler_run_on_start and not self.dry_run:
            now = now_tw()
            for job in self.jobs:
                if job.run_once_per_day:
                    continue
                if job.last_run_epoch and not self._job_eligible(job, now, once=False):
                    continue
                if job.window_start is not None and not self._job_in_window(job, now):
                    continue
                threading.Thread(
                    target=self._run_job_with_lock,
                    args=(job, now),
                    name=f"startup-{job.name}", daemon=True,
                ).start()

        tick = max(5, self.settings.scheduler_tick_seconds)
        last_status_at = 0.0
        while not self._stop.is_set():
            now = now_tw()
            try:
                for job in self.jobs:
                    self._dispatch(job, now)
                self._supervise_monitor(now)
            except Exception:
                self.logger.exception("排程主迴圈發生例外，繼續執行")
                self._write_status("degraded", last_error="scheduler tick 失敗")
            if time.time() - last_status_at >= 15:
                self._write_status("running")
                last_status_at = time.time()
            self._stop.wait(tick)

        self._write_status("stopped")
        self.logger.info("=== Scheduler 結束 ===")
        return 0

    def stop(self) -> None:
        self._stop.set()


def _is_data_window(now: dt.datetime) -> bool:
    return _is_weekday(now.date()) and _in_window(
        now.time(), _DATA_WINDOW_START, _DATA_WINDOW_END
    )


def _child_env(project_root: Optional[Path] = None) -> dict:
    import os
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if project_root is not None:
        src_path = str(project_root / "src")
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            src_path if not existing_pythonpath else src_path + os.pathsep + existing_pythonpath
        )
    return env


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(prog="stock-scheduler", description="常駐背景排程器")
    parser.add_argument("--once", action="store_true", help="到期任務各跑一次即結束")
    parser.add_argument("--dry-run", action="store_true", help="只印排程計畫，不實際執行")
    parser.add_argument(
        "--root", type=Path, default=None,
        help="專案資料／設定根目錄（未指定時使用目前工作目錄）",
    )
    args = parser.parse_args(argv)

    from bot.app_bootstrap import get_or_create_bus

    project_root = args.root.resolve() if args.root else Path.cwd()
    settings = Settings(_env_file=str(project_root / ".env"))  # type: ignore[call-arg]
    if not settings.scheduler_enabled and not args.once and not args.dry_run:
        get_logger("scheduler", log_dir=project_root / "log").warning(
            "SCHEDULER_ENABLED=false；如需常駐請設為 true，或用 --once / --dry-run。"
        )
        return 0

    process_lock = _ProcessLock(project_root)
    if not args.dry_run and not process_lock.acquire():
        get_logger("scheduler", log_dir=project_root / "log").info(
            "已有另一個 scheduler 程序持有單例鎖，略過重複啟動。"
        )
        return 75

    try:
        publisher = get_or_create_bus(project_root=project_root)
        scheduler = Scheduler(
            settings,
            project_root=project_root,
            dry_run=args.dry_run,
            publisher=publisher,
        )

        def _shutdown(signum: int, frame: object) -> None:
            scheduler.logger.info("收到信號 %d，準備關閉 ...", signum)
            scheduler.stop()

        try:
            signal.signal(signal.SIGINT, _shutdown)
            signal.signal(signal.SIGTERM, _shutdown)
        except Exception:
            pass

        return scheduler.run(once=args.once)
    finally:
        process_lock.release()


if __name__ == "__main__":
    sys.exit(main())

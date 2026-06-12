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
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_running(self) -> bool:
        locked = self._lock.acquire(blocking=False)
        if locked:
            self._lock.release()
            return False
        return True


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
        self.logger = get_logger("scheduler")
        self.log_dir = self.project_root / "log"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self.jobs: List[Job] = self._build_jobs()
        self._monitor_runner = None  # 延遲建立，避免無謂 import

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
        if s.scheduler_research_interval_min > 0:
            extra = [a for a in str(s.scheduler_research_args).split() if a]
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

    def _run_job(self, job: Job, now: Optional[dt.datetime] = None) -> None:
        scheduled_at = now or now_tw()
        cmd = _resolve_cmd(job.console_script, job.module, self._job_extra_args(job, scheduled_at))
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_path = self.log_dir / f"scheduler_{job.name}_{ts}.log"
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
            return

        env = _child_env(self.project_root)
        try:
            with open(log_path, "w", encoding="utf-8", buffering=1) as fp:
                fp.write(f"# job={job.name} cmd={' '.join(cmd)} start={ts}\n# ---\n")
                fp.flush()
                rc = subprocess.run(
                    cmd, cwd=str(self.project_root), env=env,
                    stdout=fp, stderr=subprocess.STDOUT,
                ).returncode
                fp.write(f"\n# ---\n# exit_code: {rc}\n")
            level = self.logger.info if rc == 0 else self.logger.warning
            level("■ 任務 %s 結束 (exit=%s)", job.name, rc)
        except Exception:
            self.logger.exception("任務 %s 執行失敗", job.name)
            rc = -1
        finally:
            job.last_run_epoch = time.time()
            if job.run_once_per_day:
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

    def _dispatch(self, job: Job, now: dt.datetime) -> None:
        if job.is_running():
            return
        if job.run_once_per_day:
            if not self._job_in_window(job, now):
                return
            run_key = self._daily_run_key(job, now)
            if job.last_run_key == run_key:
                return
            if self._report_exists_for_job(job, now):
                self.logger.info("skip %s; report already exists for %s", job.name, run_key)
                job.last_run_key = run_key
                job.last_run_epoch = time.time()
                return
        else:
            due = (time.time() - job.last_run_epoch) >= job.interval_min * 60
            if not due:
                return
            if job.window_start is not None and not self._job_in_window(job, now):
                return
        if job.market_hours_only and not _is_data_window(now):
            return

        def _worker() -> None:
            with job._lock:
                self._run_job(job, now)

        threading.Thread(target=_worker, name=f"job-{job.name}", daemon=True).start()

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
            return 0

        if once:
            now = now_tw()
            for job in self.jobs:
                if job.market_hours_only and not _is_data_window(now):
                    self.logger.info("略過 %s (非交易時段)", job.name)
                    continue
                self._run_job(job, now)
            self._supervise_monitor(now)
            self.logger.info("=== Scheduler --once 完成 ===")
            return 0

        # 啟動即跑一輪 (不受 market_hours_only 限制，方便即時看到資料)
        if self.settings.scheduler_run_on_start and not self.dry_run:
            now = now_tw()
            for job in self.jobs:
                if job.run_once_per_day:
                    continue
                if job.window_start is not None and not self._job_in_window(job, now):
                    continue
                threading.Thread(
                    target=lambda j=job: (j._lock.acquire(),
                                          self._run_job(j), j._lock.release()),
                    name=f"startup-{job.name}", daemon=True,
                ).start()

        tick = max(5, self.settings.scheduler_tick_seconds)
        while not self._stop.is_set():
            now = now_tw()
            try:
                for job in self.jobs:
                    self._dispatch(job, now)
                self._supervise_monitor(now)
            except Exception:
                self.logger.exception("排程主迴圈發生例外，繼續執行")
            self._stop.wait(tick)

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
    args = parser.parse_args(argv)

    from bot.app_bootstrap import get_or_create_bus

    settings = Settings()
    if not settings.scheduler_enabled and not args.once and not args.dry_run:
        get_logger("scheduler").warning(
            "SCHEDULER_ENABLED=false；如需常駐請設為 true，或用 --once / --dry-run。"
        )
        return 0

    publisher = get_or_create_bus()
    scheduler = Scheduler(
        settings,
        project_root=Path.cwd(),
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


if __name__ == "__main__":
    sys.exit(main())

"""stock-scheduler -- 常駐背景排程器。

讓「股票資訊持續自動更新 + 盤中監測」不必開著儀表板也能跑：

  * macro   : 輕量行情 / 總經刷新 (呼叫 stock-macro-update，不打 LLM)
  * research: 完整研究管線 (呼叫 stock-auto-research，含 ETF/籌碼/基本面/LLM 簡報)
  * monitor : 盤中自動托管 stock-bot 監測子行程 (開盤啟動、收盤停止)

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
from shutil import which
from typing import Callable, List, Optional

from bot.config import Settings
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
    uv = which("uv")
    if uv:
        return [uv, "run"]
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
    last_run_epoch: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_running(self) -> bool:
        locked = self._lock.acquire(blocking=False)
        if locked:
            self._lock.release()
            return False
        return True


class Scheduler:
    def __init__(self, settings: Settings, *, project_root: Optional[Path] = None,
                 dry_run: bool = False):
        self.settings = settings
        self.project_root = project_root or Path.cwd()
        self.dry_run = dry_run
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
        if getattr(s, "scheduler_company_interval_min", 0) > 0:
            jobs.append(Job(
                name="company",
                console_script="stock-company-update",
                module="bot.company_update",
                interval_min=s.scheduler_company_interval_min,
                market_hours_only=False,  # 公司基本資料為靜態，任何時段皆可補
            ))
        return jobs

    # ------------------------------------------------------------------
    # 任務執行
    # ------------------------------------------------------------------

    def _run_job(self, job: Job) -> None:
        cmd = _resolve_cmd(job.console_script, job.module, job.extra_args)
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_path = self.log_dir / f"scheduler_{job.name}_{ts}.log"
        self.logger.info("▶ 執行任務 %s: %s → %s", job.name, " ".join(cmd), log_path.name)
        if self.dry_run:
            self.logger.info("  (dry-run，略過實際執行)")
            job.last_run_epoch = time.time()
            return

        env = _child_env()
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
        finally:
            job.last_run_epoch = time.time()

    def _dispatch(self, job: Job, now: dt.datetime) -> None:
        if job.is_running():
            return
        if job.market_hours_only and not _is_data_window(now):
            return
        due = (time.time() - job.last_run_epoch) >= job.interval_min * 60
        if not due:
            return

        def _worker() -> None:
            with job._lock:
                self._run_job(job)

        threading.Thread(target=_worker, name=f"job-{job.name}", daemon=True).start()

    # ------------------------------------------------------------------
    # 盤中監測子行程托管
    # ------------------------------------------------------------------

    def _supervise_monitor(self, now: dt.datetime) -> None:
        if not self.settings.scheduler_supervise_monitor:
            return
        if self._monitor_runner is None:
            from bot.process_runner import get_runner
            self._monitor_runner = get_runner(self.project_root)

        runner = self._monitor_runner
        should_run = _is_weekday(now.date()) and _in_window(
            now.time(), _MONITOR_OPEN, _MONITOR_CLOSE
        )
        running = runner.is_running()
        if should_run and not running:
            if self.dry_run:
                self.logger.info("  (dry-run) 應啟動監測子行程 (%s)",
                                 self.settings.scheduler_monitor_mode)
                return
            rec = runner.start(run_mode=self.settings.scheduler_monitor_mode)
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
                self._run_job(job)
            self._supervise_monitor(now)
            self.logger.info("=== Scheduler --once 完成 ===")
            return 0

        # 啟動即跑一輪 (不受 market_hours_only 限制，方便即時看到資料)
        if self.settings.scheduler_run_on_start and not self.dry_run:
            for job in self.jobs:
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


def _child_env() -> dict:
    import os
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
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

    settings = Settings()
    if not settings.scheduler_enabled and not args.once and not args.dry_run:
        get_logger("scheduler").warning(
            "SCHEDULER_ENABLED=false；如需常駐請設為 true，或用 --once / --dry-run。"
        )
        return 0

    scheduler = Scheduler(settings, project_root=Path.cwd(), dry_run=args.dry_run)

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

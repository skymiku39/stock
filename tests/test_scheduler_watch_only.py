"""Scheduler must not start trade monitor when day trading archived."""
from __future__ import annotations

from bot.config import Settings
from bot.scheduler import Scheduler


def test_resolve_monitor_mode_forces_watch_when_archived():
    s = Settings(
        scheduler_monitor_mode="trade",
        day_trading_archived=True,
        day_trading_unfreeze=False,
    )
    sched = Scheduler(s, dry_run=True)
    assert sched._resolve_monitor_mode() == "watch"

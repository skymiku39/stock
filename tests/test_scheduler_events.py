"""排程器 Pub/Sub 事件測試。"""

from __future__ import annotations

import pytest

from bot.events import (
    SchedulerJobCompleted,
    SchedulerStarted,
    create_event_bus,
    reset_event_bus,
)
from bot.scheduler import Scheduler


@pytest.fixture(autouse=True)
def _reset_bus() -> None:
    reset_event_bus()
    yield
    reset_event_bus()


def _settings(**overrides):
    from bot.config import Settings

    defaults = dict(symbols=["2330"], _env_file=None)
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


class TestSchedulerEvents:
    def test_run_publishes_started_and_job_completed(self) -> None:
        bus = create_event_bus(enable_logging=False)
        received: list[str] = []

        bus.subscribe(SchedulerStarted, lambda e: received.append(f"start:{','.join(e.job_names)}"))
        bus.subscribe(
            SchedulerJobCompleted,
            lambda e: received.append(f"job:{e.job_name}:{e.dry_run}"),
        )

        sch = Scheduler(
            _settings(
                scheduler_macro_interval_min=0,
                scheduler_fundamentals_interval_min=0,
                scheduler_research_interval_min=0,
                scheduler_company_interval_min=0,
                scheduler_history_fetch_interval_min=0,
                scheduler_supervise_monitor=False,
            ),
            dry_run=True,
            publisher=bus,
        )
        sch.run(once=True)

        assert any(r.startswith("start:") for r in received)
        assert received.count("start:") == 1

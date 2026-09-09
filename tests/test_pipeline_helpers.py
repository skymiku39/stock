"""管線步驟事件輔助測試。"""

from __future__ import annotations

from bot.events import (
    InMemoryEventBus,
    PipelineProgress,
    PipelineStepCompleted,
    publish_pipeline_step,
    reset_event_bus,
)


def setup_function() -> None:
    reset_event_bus()


def teardown_function() -> None:
    reset_event_bus()


def test_publish_pipeline_step_emits_event() -> None:
    bus = InMemoryEventBus()
    received: list[PipelineStepCompleted] = []
    bus.subscribe(PipelineStepCompleted, received.append)

    publish_pipeline_step(
        bus,
        pipeline="data_research",
        step="calendar",
        step_index=0,
        total_steps=6,
        success=True,
        message="ok",
    )

    assert len(received) == 1
    assert received[0].pipeline == "data_research"
    assert received[0].step == "calendar"
    assert received[0].step_index == 0
    assert received[0].total_steps == 6
    assert received[0].success is True


def test_pipeline_progress_increments_index() -> None:
    bus = InMemoryEventBus()
    steps: list[tuple[str, int]] = []
    bus.subscribe(
        PipelineStepCompleted,
        lambda e: steps.append((e.step, e.step_index)),
    )

    progress = PipelineProgress(bus, "intraday", total_steps=3)
    progress.complete("news")
    progress.complete("macro", success=False, message="boom")
    progress.complete("brief", message="skipped")

    assert steps == [("news", 0), ("macro", 1), ("brief", 2)]

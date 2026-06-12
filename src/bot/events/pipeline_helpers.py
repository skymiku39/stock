"""管線事件發布輔助 — 避免各 pipeline 重複組裝事件。"""

from __future__ import annotations

import time
from typing import Optional

from bot.events.protocols import EventPublisher
from bot.events.types import PipelineCompleted, PipelineStepCompleted
from bot.events.wiring import publish_if_bus


def publish_pipeline_step(
    publisher: Optional[EventPublisher],
    *,
    pipeline: str,
    step: str,
    step_index: int,
    total_steps: int,
    success: bool,
    message: str = "",
    started_at: Optional[float] = None,
) -> None:
    duration = time.time() - started_at if started_at is not None else 0.0
    publish_if_bus(
        publisher,
        PipelineStepCompleted(
            pipeline=pipeline,
            step=step,
            step_index=step_index,
            total_steps=total_steps,
            success=success,
            message=message,
            duration_sec=duration,
        ),
    )


def publish_pipeline_completed(
    publisher: Optional[EventPublisher],
    *,
    pipeline: str,
    run_id: str,
    output_dir: str,
    success: bool,
    error_count: int = 0,
    duration_sec: float = 0.0,
    extra: Optional[dict] = None,
) -> None:
    publish_if_bus(
        publisher,
        PipelineCompleted(
            pipeline=pipeline,
            run_id=run_id,
            output_dir=output_dir,
            success=success,
            error_count=error_count,
            duration_sec=duration_sec,
            extra=extra or {},
        ),
    )

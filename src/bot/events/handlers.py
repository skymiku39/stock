"""可選事件 handler — 單一職責、可組合。"""

from __future__ import annotations

import json
from pathlib import Path

from bot.events.types import DomainEvent, QuantDataFetchCompleted


class JsonlEventRecorder:
    """將事件追加寫入 JSONL（稽核/除錯）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: DomainEvent) -> None:
        row = {
            "event_type": event.event_type,
            "event_id": event.event_id,
            "occurred_at": event.occurred_at,
            "payload": _event_payload(event),
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _event_payload(event: DomainEvent) -> dict:
    if isinstance(event, QuantDataFetchCompleted):
        return {
            "symbols": list(event.symbols),
            "start": event.start,
            "end": event.end,
            "price_bars": dict(event.price_bars),
            "errors": list(event.errors),
        }
    return {
        k: v
        for k, v in event.__dict__.items()
        if k not in ("event_id", "occurred_at")
    }


def attach_jsonl_recorder(
    bus,
    path: Path | None = None,
) -> JsonlEventRecorder:
    """註冊 JSONL 紀錄 handler 並回傳實例。"""
    from bot.events.wiring import get_event_bus

    target = path or Path("data/events/domain_events.jsonl")
    recorder = JsonlEventRecorder(target)
    bus = bus or get_event_bus()
    bus.subscribe_all(recorder)
    return recorder

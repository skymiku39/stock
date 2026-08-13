"""Pub/Sub 事件匯流排測試。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.events import (
    DailyKlineFetched,
    InMemoryEventBus,
    QuantDataFetchCompleted,
    SmileScreenCompleted,
    create_event_bus,
    reset_event_bus,
)
from bot.events.handlers import attach_jsonl_recorder
from bot.events.wiring import get_event_bus, publish_if_bus


@pytest.fixture(autouse=True)
def _reset_bus() -> None:
    reset_event_bus()
    yield
    reset_event_bus()


class TestInMemoryEventBus:
    def test_publish_subscribe(self) -> None:
        bus = InMemoryEventBus()
        received: list[str] = []

        bus.subscribe(DailyKlineFetched, lambda e: received.append(e.symbol))
        bus.publish(DailyKlineFetched(symbol="2303", bar_count=10, start="a", end="b"))

        assert received == ["2303"]

    def test_unsubscribe(self) -> None:
        bus = InMemoryEventBus()
        received: list[str] = []

        def handler(event) -> None:
            received.append(event.symbol)

        unsub = bus.subscribe(DailyKlineFetched, handler)
        bus.publish(DailyKlineFetched(symbol="2303", bar_count=1, start="a", end="b"))
        unsub()
        bus.publish(DailyKlineFetched(symbol="2344", bar_count=1, start="a", end="b"))

        assert received == ["2303"]

    def test_subscribe_all(self) -> None:
        bus = InMemoryEventBus()
        types: list[str] = []
        bus.subscribe_all(lambda e: types.append(e.event_type))
        bus.publish(DailyKlineFetched(symbol="2303", bar_count=1, start="a", end="b"))
        bus.publish(SmileScreenCompleted(
            total_fund=300_000, top_n=5,
            selected_symbols=("2303",), candidate_count=1,
        ))
        assert types == ["DailyKlineFetched", "SmileScreenCompleted"]

    def test_publish_if_bus_none(self) -> None:
        publish_if_bus(None, DailyKlineFetched(symbol="x", bar_count=0, start="", end=""))

    def test_singleton_bus(self) -> None:
        a = get_event_bus()
        b = get_event_bus()
        assert a is b


class TestJsonlRecorder:
    def test_writes_jsonl(self, tmp_path: Path) -> None:
        bus = create_event_bus(enable_logging=False)
        path = tmp_path / "events.jsonl"
        attach_jsonl_recorder(bus, path)
        bus.publish(QuantDataFetchCompleted(
            symbols=("2303",),
            start="2020-01-01",
            end="2026-01-01",
            price_bars={"2303": 100},
        ))
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert "QuantDataFetchCompleted" in lines[0]

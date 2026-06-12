"""InMemoryEventBus — 同步 Pub/Sub 實作 (Open/Closed: 擴充 handler 不改 bus)。"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Callable, DefaultDict, List, Optional

from bot.events.protocols import EventHandler
from bot.events.types import DomainEvent

_WILDCARD = "*"


class InMemoryEventBus:
    """執行緒安全的記憶體內事件匯流排。"""

    def __init__(self) -> None:
        self._handlers: DefaultDict[str, List[EventHandler]] = defaultdict(list)
        self._lock = threading.RLock()

    def publish(self, event: DomainEvent) -> None:
        key = event.event_type
        with self._lock:
            handlers = list(self._handlers.get(key, ()))
            handlers.extend(self._handlers.get(_WILDCARD, ()))
        for handler in handlers:
            handler(event)

    def subscribe(
        self,
        event_type: type[DomainEvent],
        handler: EventHandler,
    ) -> Callable[[], None]:
        key = event_type.__name__
        with self._lock:
            if handler not in self._handlers[key]:
                self._handlers[key].append(handler)

        def _unsubscribe() -> None:
            self.unsubscribe(event_type, handler)

        return _unsubscribe

    def subscribe_all(self, handler: EventHandler) -> Callable[[], None]:
        """訂閱所有事件型別（除錯/紀錄用）。"""
        with self._lock:
            if handler not in self._handlers[_WILDCARD]:
                self._handlers[_WILDCARD].append(handler)

        def _unsubscribe() -> None:
            with self._lock:
                self._handlers[_WILDCARD] = [
                    h for h in self._handlers[_WILDCARD] if h is not handler
                ]

        return _unsubscribe

    def unsubscribe(
        self,
        event_type: type[DomainEvent],
        handler: EventHandler,
    ) -> None:
        key = event_type.__name__
        with self._lock:
            bucket = self._handlers.get(key)
            if not bucket:
                return
            self._handlers[key] = [h for h in bucket if h is not handler]

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()

    def handler_count(self, event_type: Optional[type[DomainEvent]] = None) -> int:
        with self._lock:
            if event_type is None:
                return sum(len(v) for v in self._handlers.values())
            return len(self._handlers.get(event_type.__name__, ()))


class LoggingEventHandler:
    """橫切關注點：將事件寫入 log (SRP)。"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("events")

    def __call__(self, event: DomainEvent) -> None:
        self._logger.info(
            "[event] %s id=%s",
            event.event_type,
            event.event_id[:8],
        )

"""Pub/Sub 抽象介面 — 依賴反轉 (DIP) + 介面隔離 (ISP)。"""

from __future__ import annotations

from typing import Callable, Protocol, TypeVar, runtime_checkable

from bot.events.types import DomainEvent

E = TypeVar("E", bound=DomainEvent)
EventHandler = Callable[[DomainEvent], None]


@runtime_checkable
class EventPublisher(Protocol):
    """發布者：只負責 publish，不知道訂閱者是誰 (SRP)。"""

    def publish(self, event: DomainEvent) -> None: ...


@runtime_checkable
class EventSubscriber(Protocol):
    """訂閱者：註冊/取消對特定事件型別的處理。"""

    def subscribe(
        self,
        event_type: type[DomainEvent],
        handler: EventHandler,
    ) -> Callable[[], None]: ...

    def unsubscribe(
        self,
        event_type: type[DomainEvent],
        handler: EventHandler,
    ) -> None: ...


@runtime_checkable
class EventBus(EventPublisher, EventSubscriber, Protocol):
    """完整事件匯流排 — 發布與訂閱分離於實作層。"""

    def clear(self) -> None: ...

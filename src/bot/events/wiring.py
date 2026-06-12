"""事件匯流排組裝 — 單一組裝點 (DIP 入口)。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Set

from bot.events.bus import InMemoryEventBus, LoggingEventHandler
from bot.events.handlers import attach_jsonl_recorder
from bot.events.protocols import EventBus
from bot.events.trading_handlers import NotificationHandler, TradeRecordingHandler
from bot.events.types import (
    BotShutdownRequested,
    BotStarted,
    ClosureCompleted,
    NotifyRequested,
    RiskEntryBlocked,
    TradeBuyFilled,
    TradeSellFilled,
)

_default_bus: Optional[InMemoryEventBus] = None
_trading_wired_bus_ids: Set[int] = set()
_jsonl_attached_bus_ids: Set[int] = set()
_chain_wired_bus_ids: Set[int] = set()


def create_event_bus(*, enable_logging: bool = True) -> InMemoryEventBus:
    """建立並註冊預設橫切 handler 的新匯流排。"""
    bus = InMemoryEventBus()
    if enable_logging:
        bus.subscribe_all(LoggingEventHandler())
    return bus


def get_event_bus() -> InMemoryEventBus:
    """程序內單例匯流排（CLI / 排程器 / BOT 共用）。"""
    global _default_bus
    if _default_bus is None:
        _default_bus = create_event_bus()
    return _default_bus


def reset_event_bus() -> None:
    """測試用：重設單例與 wiring 狀態。"""
    global _default_bus
    if _default_bus is not None:
        _default_bus.clear()
    _default_bus = None
    _trading_wired_bus_ids.clear()
    _jsonl_attached_bus_ids.clear()
    _chain_wired_bus_ids.clear()


def publish_if_bus(
    bus: Optional[EventBus],
    event,
) -> None:
    """可選發布 — 呼叫端不依賴具體 bus 實作。"""
    if bus is not None:
        bus.publish(event)


def wire_trading_handlers(
    bus: EventBus,
    *,
    recorder,
    notifier,
    risk=None,
) -> None:
    """註冊交易側效應 handler（通知 / 紀錄）。

    風控狀態由 ``BaseStrategy`` 同步更新；``risk`` 參數保留供未來擴充。
    """
    del risk  # 保留簽名以維持呼叫端相容
    bus_id = id(bus)
    if bus_id in _trading_wired_bus_ids:
        return

    notify = NotificationHandler(notifier)
    record = TradeRecordingHandler(recorder)

    for event_type in (
        BotStarted,
        NotifyRequested,
        RiskEntryBlocked,
        TradeBuyFilled,
        TradeSellFilled,
        ClosureCompleted,
        BotShutdownRequested,
    ):
        bus.subscribe(event_type, notify)

    for event_type in (TradeBuyFilled, TradeSellFilled):
        bus.subscribe(event_type, record)

    _trading_wired_bus_ids.add(bus_id)


def wire_chain_handlers(
    bus: EventBus,
    *,
    enable_smile_screen: bool = False,
    project_root: Optional[Path] = None,
) -> None:
    """註冊可選事件鏈（依設定啟用）。"""
    if not enable_smile_screen:
        return
    bus_id = id(bus)
    if bus_id in _chain_wired_bus_ids:
        return

    from bot.events.chain_handlers import SmileScreenOnDataReadyHandler
    from bot.events.types import QuantDataFetchCompleted

    bus.subscribe(
        QuantDataFetchCompleted,
        SmileScreenOnDataReadyHandler(
            project_root=project_root,
            publisher=bus,
        ),
    )
    _chain_wired_bus_ids.add(bus_id)


def wire_application_handlers(
    bus: Optional[EventBus] = None,
    *,
    jsonl_path: Optional[Path] = None,
    enable_jsonl: bool = True,
    enable_chain_smile_screen: bool = False,
    project_root: Optional[Path] = None,
) -> InMemoryEventBus:
    """應用程式層預設 wiring：logging + JSONL 稽核 + 可選事件鏈。"""
    resolved = bus if isinstance(bus, InMemoryEventBus) else get_event_bus()
    bus_id = id(resolved)

    if enable_jsonl and bus_id not in _jsonl_attached_bus_ids:
        path = jsonl_path or Path("data/events/domain_events.jsonl")
        attach_jsonl_recorder(resolved, path)
        _jsonl_attached_bus_ids.add(bus_id)

    wire_chain_handlers(
        resolved,
        enable_smile_screen=enable_chain_smile_screen,
        project_root=project_root,
    )

    return resolved


__all__ = [
    "create_event_bus",
    "get_event_bus",
    "publish_if_bus",
    "reset_event_bus",
    "wire_application_handlers",
    "wire_chain_handlers",
    "wire_trading_handlers",
]

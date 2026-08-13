"""events -- Publish/Subscribe 領域事件匯流排 (SOLID)。

設計原則
========
* **S** — `InMemoryEventBus` 只負責路由；`LoggingEventHandler` 只負責紀錄
* **O** — 新增事件型別 / handler 不需修改 bus 核心
* **L** — 所有 handler 可替換為符合 `EventHandler` 的 callable
* **I** — `EventPublisher` / `EventSubscriber` 分離介面
* **D** — 業務模組依賴 `EventPublisher` protocol，不依賴具體 bus
"""

from bot.events.bus import InMemoryEventBus, LoggingEventHandler
from bot.events.pipeline_helpers import (
    publish_pipeline_completed,
    publish_pipeline_step,
)
from bot.events.protocols import EventBus, EventHandler, EventPublisher, EventSubscriber
from bot.events.trading_handlers import (
    NotificationHandler,
    RiskPostTradeHandler,
    TradeRecordingHandler,
)
from bot.events.types import (
    BotShutdownRequested,
    BotStarted,
    ClosureCompleted,
    DailyKlineFetched,
    DomainEvent,
    NotifyRequested,
    PipelineCompleted,
    PipelineStepCompleted,
    QuantDataFetchCompleted,
    RiskEntryBlocked,
    SchedulerJobCompleted,
    SchedulerStarted,
    SignalRecorded,
    SmileAuditCompleted,
    SmileScreenCompleted,
    TickReceived,
    TradeBuyFilled,
    TradeSellFilled,
)
from bot.events.wiring import (
    create_event_bus,
    get_event_bus,
    publish_if_bus,
    reset_event_bus,
    wire_application_handlers,
    wire_trading_handlers,
)

__all__ = [
    "BotShutdownRequested",
    "BotStarted",
    "ClosureCompleted",
    "DailyKlineFetched",
    "DomainEvent",
    "EventBus",
    "EventHandler",
    "EventPublisher",
    "EventSubscriber",
    "InMemoryEventBus",
    "LoggingEventHandler",
    "NotificationHandler",
    "NotifyRequested",
    "PipelineCompleted",
    "PipelineStepCompleted",
    "QuantDataFetchCompleted",
    "RiskEntryBlocked",
    "RiskPostTradeHandler",
    "SchedulerJobCompleted",
    "SchedulerStarted",
    "SignalRecorded",
    "SmileAuditCompleted",
    "SmileScreenCompleted",
    "TickReceived",
    "TradeBuyFilled",
    "TradeRecordingHandler",
    "TradeSellFilled",
    "create_event_bus",
    "get_event_bus",
    "publish_if_bus",
    "publish_pipeline_completed",
    "publish_pipeline_step",
    "reset_event_bus",
    "wire_application_handlers",
    "wire_trading_handlers",
]

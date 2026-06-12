"""交易領域事件 handler — 單一職責訂閱者 (SRP)。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bot.events.types import (
    BotShutdownRequested,
    BotStarted,
    ClosureCompleted,
    NotifyRequested,
    RiskEntryBlocked,
    TradeBuyFilled,
    TradeSellFilled,
)

if TYPE_CHECKING:
    from bot.notifier import TelegramNotifier
    from bot.recorder import TradeRecorder
    from bot.risk_guard import RiskGuard


class NotificationHandler:
    """訂閱通知類事件 → TelegramNotifier (SRP)。"""

    def __init__(self, notifier: TelegramNotifier) -> None:
        self._notifier = notifier

    def __call__(self, event) -> None:
        if isinstance(event, BotStarted):
            self._notifier.notify_start(
                list(event.symbols),
                event.simulation,
                event.run_mode,
            )
        elif isinstance(event, NotifyRequested):
            self._notifier.send(event.text)
        elif isinstance(event, RiskEntryBlocked):
            self._notifier.send(
                f"⛔ 進場被風控擋下 {event.symbol}：{event.reason}",
            )
        elif isinstance(event, TradeBuyFilled):
            self._notifier.notify_buy(
                event.symbol, event.price, event.quantity,
            )
        elif isinstance(event, TradeSellFilled):
            self._notifier.notify_sell(
                event.symbol,
                event.price,
                event.quantity,
                event.trade_reason,
            )
        elif isinstance(event, ClosureCompleted):
            self._notifier.notify_closure(event.trade_summary)
        elif isinstance(event, BotShutdownRequested):
            self._notifier.notify_shutdown()


class TradeRecordingHandler:
    """訂閱成交事件 → TradeRecorder (SRP)。"""

    def __init__(self, recorder: TradeRecorder) -> None:
        self._recorder = recorder

    def __call__(self, event) -> None:
        if isinstance(event, TradeBuyFilled):
            self._recorder.record_deal(
                event.order_msg,
                unit=event.unit,
                trade_reason=event.trade_reason,
            )
        elif isinstance(event, TradeSellFilled):
            self._recorder.record_deal(
                event.order_msg,
                unit=event.unit,
                trade_reason=event.trade_reason,
                entry_price=event.entry_price,
                pnl_pct=event.pnl_pct,
                pnl_twd=event.pnl_twd,
            )


class RiskPostTradeHandler:
    """訂閱成交事件 → RiskGuard 資金/部位狀態更新 (SRP)。"""

    def __init__(self, risk: RiskGuard) -> None:
        self._risk = risk

    def __call__(self, event) -> None:
        if isinstance(event, TradeBuyFilled):
            self._risk.on_entry_filled(
                event.symbol,
                event.price,
                event.quantity,
                unit=event.unit,
            )
        elif isinstance(event, TradeSellFilled):
            self._risk.on_exit_filled(
                event.symbol,
                event.entry_price,
                event.price,
                event.quantity,
                unit=event.unit,
                position_closed=event.position_closed,
            )

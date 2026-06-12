"""交易事件 handler 測試 — 驗證 Pub/Sub 側效應解耦。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot.events import (
    BotStarted,
    RiskEntryBlocked,
    TradeBuyFilled,
    create_event_bus,
    reset_event_bus,
    wire_trading_handlers,
)
from bot.events.trading_handlers import NotificationHandler, TradeRecordingHandler


@pytest.fixture(autouse=True)
def _reset_bus() -> None:
    reset_event_bus()
    yield
    reset_event_bus()


class TestTradingHandlers:
    def test_notification_handler_bot_started(self) -> None:
        notifier = MagicMock()
        handler = NotificationHandler(notifier)
        handler(BotStarted(symbols=("2303",), run_mode="trade", simulation=True))
        notifier.notify_start.assert_called_once_with(["2303"], True, "trade")

    def test_trade_recording_handler_buy(self) -> None:
        recorder = MagicMock()
        handler = TradeRecordingHandler(recorder)
        msg = {"code": "2303", "price": 50.0, "quantity": 1}
        handler(TradeBuyFilled(
            symbol="2303", price=50.0, quantity=1, unit="lot",
            order_msg=msg, trade_reason="enter",
        ))
        recorder.record_deal.assert_called_once_with(
            msg, unit="lot", trade_reason="enter",
        )

    def test_wire_trading_handlers_idempotent(self) -> None:
        bus = create_event_bus(enable_logging=False)
        notifier = MagicMock()
        recorder = MagicMock()
        risk = MagicMock()
        wire_trading_handlers(bus, recorder=recorder, notifier=notifier, risk=risk)
        wire_trading_handlers(bus, recorder=recorder, notifier=notifier, risk=risk)
        bus.publish(RiskEntryBlocked(
            symbol="2330", reason="test", blocking_rule="max_fund",
        ))
        assert notifier.send.call_count == 1

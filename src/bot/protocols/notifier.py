"""NotifierProtocol -- 推播通知介面 (DIP)。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class NotifierProtocol(Protocol):
    """通知通道抽象 — 可由 Telegram、事件 handler 等實作。"""

    @property
    def enabled(self) -> bool: ...

    def send(self, text: str) -> None: ...

    def notify_start(
        self,
        symbols: list[str],
        simulation: bool,
        run_mode: str = "trade",
    ) -> None: ...

    def notify_buy(self, symbol: str, price: float, qty: int) -> None: ...

    def notify_sell(self, symbol: str, price: float, qty: int, reason: str) -> None: ...

    def notify_closure(self, summary: str) -> None: ...

    def notify_shutdown(self) -> None: ...

"""BrokerProtocol -- 下單與行情介面 (DIP)。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BrokerProtocol(Protocol):
    """券商連線抽象 — 策略層只依賴此介面。"""

    def login(self) -> bool: ...

    def logout(self) -> None: ...

    def get_contract(self, symbol: str) -> Any: ...

    def subscribe_tick(self, symbol: str) -> None: ...

    def unsubscribe(self, symbol: str) -> None: ...

    def get_snapshots(self, symbols: list[str]) -> dict[str, float]: ...

    def get_available_balance(self) -> float | None: ...

    def place_order(
        self,
        symbol: str,
        action: Any,
        quantity: int,
        price: float,
        custom_field: str = "",
    ) -> Any: ...

    def place_odd_lot_order(
        self,
        symbol: str,
        action: Any,
        shares: int,
        price: float,
        custom_field: str = "",
    ) -> Any: ...

    def place_market_sell(
        self,
        symbol: str,
        quantity: int,
        custom_field: str = "",
    ) -> Any: ...

    def set_on_tick(self, callback: Callable[..., None]) -> None: ...

    def set_on_order(self, callback: Callable[..., None]) -> None: ...

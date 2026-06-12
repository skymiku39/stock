"""MarketSourceProtocol -- 公開延遲行情介面 (DIP)。"""

from __future__ import annotations

from typing import Callable, Dict, List, Protocol, runtime_checkable

from bot.models import MarketTick


@runtime_checkable
class MarketSourceProtocol(Protocol):
    """非 Shioaji 行情來源抽象。"""

    def get_prev_close(self, symbols: List[str]) -> Dict[str, float]: ...

    def poll(self) -> List[MarketTick]: ...

    def set_on_tick(self, callback: Callable[..., None]) -> None: ...

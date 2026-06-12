"""TradeRecorderProtocol -- 成交紀錄介面 (DIP)。"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class TradeRecorderProtocol(Protocol):
    """交易紀錄抽象。"""

    def record_deal(
        self,
        msg: Dict[str, Any],
        *,
        unit: str = "lot",
        trade_reason: str = "",
        entry_price: Optional[float] = None,
        pnl_pct: float = 0.0,
        pnl_twd: float = 0.0,
    ) -> None: ...

    def export_csv(self) -> None: ...

    def summary(self) -> str: ...

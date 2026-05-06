from __future__ import annotations

import datetime
from dataclasses import dataclass, field


@dataclass
class PositionInfo:
    """單一商品的持倉資訊。"""

    symbol: str
    avg_price: float
    quantity: int
    entry_time: datetime.datetime = field(default_factory=datetime.datetime.now)

    def update(self, filled_price: float, filled_qty: int) -> None:
        """加碼時更新均價與總量。"""
        total_cost = self.avg_price * self.quantity + filled_price * filled_qty
        self.quantity += filled_qty
        self.avg_price = total_cost / self.quantity if self.quantity else 0.0

    def reduce(self, filled_qty: int) -> bool:
        """減碼，回傳 True 代表部位已清空。"""
        self.quantity -= filled_qty
        return self.quantity <= 0


@dataclass
class OrderRecord:
    """追蹤已送出但尚未完全成交的委託單。"""

    order_no: str
    symbol: str
    action: str  # "Buy" / "Sell"
    category: str  # "enter" / "stop" / "close"
    created_at: datetime.datetime = field(default_factory=datetime.datetime.now)

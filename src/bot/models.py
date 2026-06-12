from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Literal

from bot.ownership import BOT_OWNER_TAG

QtyUnit = Literal["lot", "share"]


def qty_multiplier(unit: QtyUnit) -> int:
    """1 張 = 1000 股；零股 unit=share 時乘數為 1。"""
    return 1000 if unit == "lot" else 1


@dataclass
class PositionInfo:
    """單一商品的持倉資訊。"""

    symbol: str
    avg_price: float
    quantity: int
    owner_tag: str = BOT_OWNER_TAG
    unit: QtyUnit = "lot"
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


# ------------------------------------------------------------------
# 正規化行情 / 訊號型別 (trade / watch / report 共用)
# ------------------------------------------------------------------


@dataclass
class MarketTick:
    """跨來源正規化 Tick，由 Shioaji 即時行情或 TWSE 公開延遲資料轉換而來。"""

    ts: datetime.datetime
    symbol: str
    price: float
    volume: int
    pct_chg: float
    prev_close: float
    source: str  # "shioaji" | "twse_public"


@dataclass
class SignalEvent:
    """策略觸發的交易意圖 (watch/report 模式記錄用)。"""

    ts: datetime.datetime
    symbol: str
    action: str  # "would-buy" | "would-sell" | "sell-blocked"
    price: float
    quantity: int
    reason: str  # "enter" | "sl" | "trail" | "close"
    pct_chg: float
    pnl_pct: float
    mode: str  # "watch" | "report"
    source: str  # "shioaji" | "twse_public"
    unit: QtyUnit = "lot"
    llm_gate: str = ""

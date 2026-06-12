"""protocols -- 依賴反轉 (DIP) 介面定義。

業務模組依賴這些 Protocol，而非具體實作類別。
"""

from bot.protocols.broker import BrokerProtocol
from bot.protocols.market_source import MarketSourceProtocol
from bot.protocols.notifier import NotifierProtocol
from bot.protocols.recorder import TradeRecorderProtocol

__all__ = [
    "BrokerProtocol",
    "MarketSourceProtocol",
    "NotifierProtocol",
    "TradeRecorderProtocol",
]

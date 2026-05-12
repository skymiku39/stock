"""SjBroker -- Shioaji 連線管理層。

負責登入/登出、合約取得、行情訂閱、下單封裝，
以及斷線重連等基礎設施，讓 Strategy 層無需直接操作 Shioaji API。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

import shioaji as sj
from shioaji import BidAskSTKv1, Exchange, TickSTKv1
from shioaji.constant import (
    Action,
    OrderState,
    OrderType,
    QuoteType,
    StockOrderLot,
    StockPriceType,
)

from bot.utils import get_logger

if TYPE_CHECKING:
    from shioaji.contracts import Contract
    from shioaji.order import Trade

    from bot.config import Settings


class SjBroker:
    """封裝 Shioaji SDK 的所有低階操作。"""

    def __init__(self, settings: Settings, logger: Optional[logging.Logger] = None):
        self.settings = settings
        self.logger = logger or get_logger("broker")
        self.api: Optional[sj.Shioaji] = None

        self._subscribed_symbols: List[str] = []
        self._contracts: Dict[str, Contract] = {}

        self._tick_callback: Optional[Callable] = None
        self._bidask_callback: Optional[Callable] = None
        self._order_callback: Optional[Callable] = None

        self._reconnect_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 登入 / 登出
    # ------------------------------------------------------------------

    def login(self) -> bool:
        self.logger.info("正在初始化 Shioaji ...")
        self.api = sj.Shioaji(simulation=self.settings.simulation)

        self.logger.info(
            "登入中 ... (simulation=%s)", self.settings.simulation
        )
        try:
            accounts = self.api.login(
                api_key=self.settings.api_key,
                secret_key=self.settings.secret_key,
            )
        except Exception:
            self.logger.exception("登入失敗")
            return False

        self.logger.info("登入成功，可用帳號: %s", accounts)

        if self.settings.ca_path and not self.settings.simulation:
            self.logger.info("啟用電子憑證 ...")
            self.api.activate_ca(
                ca_path=self.settings.ca_path,
                ca_passwd=self.settings.ca_password,
                person_id=self.settings.person_id,
            )
            self.logger.info("憑證啟用完成")

        self._setup_event_callbacks()
        return True

    def logout(self) -> None:
        if self.api is not None:
            self.logger.info("登出中 ...")
            try:
                self.api.logout()
            except Exception:
                self.logger.exception("登出時發生例外")
            finally:
                self.api = None
                self.logger.info("已登出")

    # ------------------------------------------------------------------
    # 合約
    # ------------------------------------------------------------------

    def get_contract(self, symbol: str) -> Optional[Contract]:
        """取得股票合約物件，並快取結果。"""
        if symbol in self._contracts:
            return self._contracts[symbol]

        assert self.api is not None, "尚未登入"
        contract = self.api.Contracts.Stocks.get(symbol)
        if contract is None:
            self.logger.error("找不到合約: %s", symbol)
            return None

        self._contracts[symbol] = contract
        return contract

    # ------------------------------------------------------------------
    # 行情訂閱
    # ------------------------------------------------------------------

    def subscribe_tick(self, symbol: str) -> None:
        contract = self.get_contract(symbol)
        if contract is None:
            return

        assert self.api is not None
        self.api.quote.subscribe(contract, quote_type=QuoteType.Tick)
        if symbol not in self._subscribed_symbols:
            self._subscribed_symbols.append(symbol)
        self.logger.info("已訂閱 Tick: %s", symbol)

    def subscribe_bidask(self, symbol: str) -> None:
        contract = self.get_contract(symbol)
        if contract is None:
            return

        assert self.api is not None
        self.api.quote.subscribe(contract, quote_type=QuoteType.BidAsk)
        self.logger.info("已訂閱 BidAsk: %s", symbol)

    def unsubscribe(self, symbol: str) -> None:
        contract = self.get_contract(symbol)
        if contract is None:
            return

        assert self.api is not None
        try:
            self.api.quote.unsubscribe(contract, quote_type=QuoteType.Tick)
            self.api.quote.unsubscribe(contract, quote_type=QuoteType.BidAsk)
        except Exception:
            self.logger.exception("取消訂閱失敗: %s", symbol)

        if symbol in self._subscribed_symbols:
            self._subscribed_symbols.remove(symbol)
        self.logger.info("已取消訂閱: %s", symbol)

    def _resubscribe_all(self) -> None:
        """斷線重連後重新訂閱所有商品。"""
        for symbol in list(self._subscribed_symbols):
            try:
                self.subscribe_tick(symbol)
                time.sleep(0.1)
            except Exception:
                self.logger.exception("重新訂閱失敗: %s", symbol)

    # ------------------------------------------------------------------
    # 回呼設定
    # ------------------------------------------------------------------

    def set_on_tick(self, callback: Callable[[Exchange, TickSTKv1], None]) -> None:
        self._tick_callback = callback

    def set_on_bidask(self, callback: Callable[[Exchange, BidAskSTKv1], None]) -> None:
        self._bidask_callback = callback

    def set_on_order(self, callback: Callable[[OrderState, dict], None]) -> None:
        self._order_callback = callback

    def _setup_event_callbacks(self) -> None:
        assert self.api is not None

        @self.api.on_tick_stk_v1()
        def _on_tick(exchange: Exchange, tick: TickSTKv1) -> None:
            if self._tick_callback:
                self._tick_callback(exchange, tick)

        @self.api.on_bidask_stk_v1()
        def _on_bidask(exchange: Exchange, bidask: BidAskSTKv1) -> None:
            if self._bidask_callback:
                self._bidask_callback(exchange, bidask)

        @self.api.quote.on_event
        def _on_event(
            resp_code: int, event_code: int, info: str, event: str
        ) -> None:
            self.logger.debug(
                "Quote event: resp=%d code=%d info=%s event=%s",
                resp_code, event_code, info, event,
            )
            if event_code in (1, 2):
                # 1=Disconnected, 2=Connect failed -> 嘗試重新訂閱
                self._handle_reconnect()
            elif event_code in (3, 4):
                # 3=Reconnecting, 4=Reconnected
                if event_code == 4:
                    self.logger.info("行情連線已恢復，重新訂閱 ...")
                    self._resubscribe_all()

        self.api.set_order_callback(self._dispatch_order_callback)

    def _dispatch_order_callback(self, stat: OrderState, msg: dict) -> None:
        self.logger.debug("Order callback: stat=%s msg=%s", stat, msg)
        if self._order_callback:
            self._order_callback(stat, msg)

    def _handle_reconnect(self) -> None:
        """行情連線異常時嘗試重新登入並恢復訂閱 (指數退避)。"""
        if not self._reconnect_lock.acquire(blocking=False):
            return

        def _reconnect_worker() -> None:
            max_retries = 10
            delay = 5.0
            try:
                for attempt in range(1, max_retries + 1):
                    self.logger.warning(
                        "嘗試重連 (%d/%d)，等待 %.0f 秒 ...",
                        attempt, max_retries, delay,
                    )
                    time.sleep(delay)

                    try:
                        if self.api is not None:
                            try:
                                self.api.logout()
                            except Exception:
                                pass

                        self.api = sj.Shioaji(simulation=self.settings.simulation)
                        self.api.login(
                            api_key=self.settings.api_key,
                            secret_key=self.settings.secret_key,
                        )

                        if self.settings.ca_path and not self.settings.simulation:
                            self.api.activate_ca(
                                ca_path=self.settings.ca_path,
                                ca_passwd=self.settings.ca_password,
                                person_id=self.settings.person_id,
                            )

                        self._setup_event_callbacks()
                        self._resubscribe_all()
                        self.logger.info("重連成功!")
                        return

                    except Exception:
                        self.logger.exception("重連失敗 (第 %d 次)", attempt)
                        delay = min(delay * 2, 120)

                self.logger.error("已達重連上限 (%d 次)，放棄重連", max_retries)
            finally:
                self._reconnect_lock.release()

        threading.Thread(
            target=_reconnect_worker, daemon=True, name="reconnect",
        ).start()

    # ------------------------------------------------------------------
    # 下單
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        action: Action,
        quantity: int,
        price: float = 0,
        price_type: StockPriceType = StockPriceType.LMT,
        order_type: OrderType = OrderType.ROD,
        custom_field: str = "",
    ) -> Optional[Trade]:
        contract = self.get_contract(symbol)
        if contract is None:
            return None

        assert self.api is not None
        order = self.api.Order(
            price=price,
            quantity=quantity,
            action=action,
            price_type=price_type,
            order_type=order_type,
            order_lot=StockOrderLot.Common,
            custom_field=custom_field[:6],
            account=self.api.stock_account,
        )

        self.logger.info(
            "下單: %s %s %d 張 @ %s (%s/%s) [%s]",
            action.value, symbol, quantity, price,
            price_type.value, order_type.value, custom_field,
        )

        try:
            trade = self.api.place_order(contract, order)
            self.logger.info(
                "下單結果: id=%s status=%s",
                trade.order.id, trade.status.status,
            )
            return trade
        except Exception:
            self.logger.exception("下單失敗: %s", symbol)
            return None

    def place_market_sell(
        self,
        symbol: str,
        quantity: int,
        custom_field: str = "close",
    ) -> Optional[Trade]:
        """市價 IOC 賣出 (用於停損/全出場)。"""
        return self.place_order(
            symbol=symbol,
            action=Action.Sell,
            quantity=quantity,
            price=0,
            price_type=StockPriceType.MKT,
            order_type=OrderType.IOC,
            custom_field=custom_field,
        )

    # ------------------------------------------------------------------
    # 快照 / 前日收盤
    # ------------------------------------------------------------------

    def get_snapshots(self, symbols: list[str]) -> Dict[str, float]:
        """取得多檔商品的前日收盤 (reference) 價。

        優先使用合約物件的 reference 屬性，若為 0 則透過 snapshots API
        以 close - change_price 推算。
        """
        assert self.api is not None, "尚未登入"
        result: Dict[str, float] = {}

        contracts = []
        for s in symbols:
            c = self.get_contract(s)
            if c is None:
                continue
            ref = getattr(c, "reference", 0.0)
            if ref and float(ref) > 0:
                result[s] = float(ref)
            else:
                contracts.append(c)

        if contracts:
            try:
                snaps = self.api.snapshots(contracts)
                for snap in snaps:
                    code = snap.code
                    ref = float(snap.close) - float(snap.change_price)
                    if ref > 0:
                        result[code] = ref
            except Exception:
                self.logger.exception("取得 snapshots 失敗")

        self.logger.info("前日收盤價: %s", result)
        return result

    # ------------------------------------------------------------------
    # 訂單狀態查詢
    # ------------------------------------------------------------------

    def update_status(self) -> None:
        assert self.api is not None
        self.api.update_status(self.api.stock_account)

    def list_trades(self) -> list:
        assert self.api is not None
        return self.api.list_trades()

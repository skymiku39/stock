"""SjBroker -- Shioaji 連線管理層。

負責登入/登出、合約取得、行情訂閱、下單封裝，
以及斷線重連等基礎設施，讓 Strategy 層無需直接操作 Shioaji API。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

import shioaji as sj
from shioaji import BidAskSTKv1, Exchange, TickSTKv1
from shioaji.constant import (
    Action,
    OrderState,
    OrderType,
    QuoteType,
    StockOrderCond,
    StockOrderLot,
    StockPriceType,
)

from bot.utils import get_logger
from bot.ownership import clean_order_field

if TYPE_CHECKING:
    from shioaji.contracts import Contract
    from shioaji.order import Order, Trade

    from bot.config import Settings


CONTRACTS_TIMEOUT_MS = 10_000
BALANCE_CACHE_TTL_SEC = 30


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
        self._subscriptions: set[Tuple[str, str]] = set()

        self._reconnect_lock = threading.Lock()
        self.last_order_error: Optional[Exception] = None
        self._balance_cache: Optional[Tuple[float, float]] = None  # (monotonic_ts, amount)

    def _should_activate_ca(self) -> bool:
        """Only trade mode may activate CA; watch mode must stay quote-only."""
        return bool(
            self.settings.ca_path
            and not self.settings.simulation
            and self.settings.run_mode == "trade"
        )

    def _subscribe(self, contract: "Contract", quote_type: QuoteType) -> None:
        assert self.api is not None
        if hasattr(self.api, "subscribe"):
            try:
                self.api.subscribe(contract, quote_type=quote_type)
                return
            except TypeError:
                self.api.subscribe(contract, quote_type=_quote_type_label(quote_type))
                return
        self.api.quote.subscribe(contract, quote_type=quote_type)

    def _unsubscribe(self, contract: "Contract", quote_type: QuoteType) -> None:
        assert self.api is not None
        if hasattr(self.api, "unsubscribe"):
            try:
                self.api.unsubscribe(contract, quote_type=quote_type)
                return
            except TypeError:
                self.api.unsubscribe(contract, quote_type=_quote_type_label(quote_type))
                return
        self.api.quote.unsubscribe(contract, quote_type=quote_type)

    # ------------------------------------------------------------------
    # 登入 / 登出
    # ------------------------------------------------------------------

    def login(self) -> bool:
        self.logger.info("正在初始化 Shioaji ...")
        self.api = sj.Shioaji(simulation=self.settings.simulation)

        self.logger.info(
            "登入中 ... (simulation=%s, run_mode=%s)",
            self.settings.simulation,
            self.settings.run_mode,
        )
        try:
            accounts = self.api.login(
                api_key=self.settings.api_key,
                secret_key=self.settings.secret_key,
                contracts_timeout=CONTRACTS_TIMEOUT_MS,
            )
        except Exception as exc:
            msg = str(exc)
            low = msg.lower()
            if "not allow" in low and "ip" in low:
                import re as _re
                m = _re.search(r"ip:\s*([0-9a-fA-F:.]+)", msg)
                bad_ip = m.group(1) if m else "目前對外 IP"
                self.logger.error(
                    "登入失敗：IP 白名單阻擋 (%s 不在金鑰允許清單)。"
                    "請到永豐 iLeader → API 金鑰管理移除 IP 限制或加入該 IP。", bad_ip,
                )
            elif "permission" in low or "401" in low:
                self.logger.error(
                    "登入/權限失敗：API 金鑰可能未開通『下單』權限。詳: %s", msg[:120],
                )
            else:
                self.logger.exception("登入失敗")
            return False

        self.logger.info("登入成功，可用帳號: %s", accounts)

        if self._should_activate_ca():
            self.logger.info("啟用電子憑證 ...")
            self.api.activate_ca(
                ca_path=self.settings.ca_path,
                ca_passwd=self.settings.ca_password,
                person_id=self.settings.person_id,
            )
            self.logger.info("憑證啟用完成")
        elif self.settings.run_mode == "watch":
            self.logger.info("watch 模式 — 跳過電子憑證啟用")

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

    def get_available_balance(self, *, force_refresh: bool = False) -> Optional[float]:
        """讀取證券帳戶可用餘額 (acc_balance)；失敗回傳 None。"""
        if self.api is None:
            return None
        now = time.monotonic()
        if (
            not force_refresh
            and self._balance_cache is not None
            and now - self._balance_cache[0] < BALANCE_CACHE_TTL_SEC
        ):
            return self._balance_cache[1]
        try:
            balance = self.api.account_balance()
            if not balance:
                return None
            amount = float(getattr(balance, "acc_balance", 0) or 0)
            self._balance_cache = (now, amount)
            return amount
        except Exception as exc:
            self.logger.warning("讀取 account_balance 失敗: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 合約
    # ------------------------------------------------------------------

    def get_contract(self, symbol: str) -> Optional[Contract]:
        """取得股票合約物件，並快取結果。"""
        if symbol in self._contracts:
            return self._contracts[symbol]

        assert self.api is not None, "尚未登入"
        contract = _lookup_stock_contract(self.api, symbol)
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
        self._subscribe(contract, QuoteType.Tick)
        self._subscriptions.add((symbol, "tick"))
        if symbol not in self._subscribed_symbols:
            self._subscribed_symbols.append(symbol)
        self.logger.info("已訂閱 Tick: %s", symbol)

    def subscribe_bidask(self, symbol: str) -> None:
        contract = self.get_contract(symbol)
        if contract is None:
            return

        assert self.api is not None
        self._subscribe(contract, QuoteType.BidAsk)
        self._subscriptions.add((symbol, "bidask"))
        if symbol not in self._subscribed_symbols:
            self._subscribed_symbols.append(symbol)
        self.logger.info("已訂閱 BidAsk: %s", symbol)

    def unsubscribe(self, symbol: str) -> None:
        contract = self.get_contract(symbol)
        if contract is None:
            return

        assert self.api is not None
        try:
            self._unsubscribe(contract, QuoteType.Tick)
            self._unsubscribe(contract, QuoteType.BidAsk)
        except Exception:
            self.logger.exception("取消訂閱失敗: %s", symbol)

        self._subscriptions = {
            item for item in self._subscriptions if item[0] != symbol
        }
        if symbol in self._subscribed_symbols:
            self._subscribed_symbols.remove(symbol)
        self.logger.info("已取消訂閱: %s", symbol)

    def _resubscribe_all(self) -> None:
        """斷線重連後重新訂閱所有商品。"""
        subscriptions = sorted(self._subscriptions)
        if not subscriptions:
            subscriptions = [(symbol, "tick") for symbol in self._subscribed_symbols]
        for symbol, qtype in subscriptions:
            try:
                if qtype == "bidask":
                    self.subscribe_bidask(symbol)
                else:
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
                            contracts_timeout=CONTRACTS_TIMEOUT_MS,
                        )

                        if self._should_activate_ca():
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
        order_lot: StockOrderLot = StockOrderLot.Common,
        max_sell_qty: Optional[int] = None,
    ) -> Optional[Trade]:
        if self.settings.run_mode != "trade":
            self.logger.error(
                "非 trade 模式 (%s) 禁止下單 — 已攔截",
                self.settings.run_mode,
            )
            return None

        if action == Action.Sell:
            if quantity <= 0:
                self.logger.error("賣單數量無效: %s qty=%d", symbol, quantity)
                return None
            if max_sell_qty is not None and quantity > max_sell_qty:
                self.logger.error(
                    "僅做多：賣單 %d 超過持倉 %d (%s)，已攔截",
                    quantity, max_sell_qty, symbol,
                )
                return None

        contract = self.get_contract(symbol)
        if contract is None:
            return None

        assert self.api is not None
        order = self._build_stock_order(
            price=price,
            quantity=quantity,
            action=action,
            price_type=price_type,
            order_type=order_type,
            custom_field=custom_field,
            order_lot=order_lot,
        )

        unit = "股" if order_lot in (StockOrderLot.IntradayOdd, StockOrderLot.Odd) else "張"
        self.logger.info(
            "下單: %s %s %d %s @ %s (%s/%s/%s) [%s]",
            action.value, symbol, quantity, unit, price,
            price_type.value, order_type.value, order_lot.value, custom_field,
        )

        try:
            trade = self.api.place_order(contract, order)
            self.last_order_error = None
            self.logger.info(
                "下單結果: id=%s status=%s",
                trade.order.id, trade.status.status,
            )
            return trade
        except Exception as exc:
            self.last_order_error = exc
            self.logger.exception("下單失敗: %s", symbol)
            return None

    def _build_stock_order(
        self,
        *,
        price: float,
        quantity: int,
        action: Action,
        price_type: StockPriceType,
        order_type: OrderType,
        custom_field: str,
        order_lot: StockOrderLot = StockOrderLot.Common,
    ) -> "Order":
        assert self.api is not None
        kwargs = dict(
            price=price,
            quantity=quantity,
            action=action,
            price_type=price_type,
            order_type=order_type,
            order_lot=order_lot,
            order_cond=StockOrderCond.Cash,
            custom_field=_clean_custom_field(custom_field),
            account=self.api.stock_account,
        )
        stock_order_cls = getattr(sj, "StockOrder", None)
        if stock_order_cls is not None:
            return stock_order_cls(**kwargs)
        return self.api.Order(**kwargs)

    def place_market_sell(
        self,
        symbol: str,
        quantity: int,
        custom_field: str = "close",
        max_sell_qty: Optional[int] = None,
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
            max_sell_qty=max_sell_qty,
        )

    def place_odd_lot_order(
        self,
        symbol: str,
        action: Action,
        shares: int,
        price: float,
        custom_field: str = "odd",
        max_sell_qty: Optional[int] = None,
    ) -> Optional[Trade]:
        """盤中零股委託 (IntradayOdd)。

        shares 為「股數」(1~999)，price 必為限價 (零股不支援市價)。
        盤中零股交易時段為 09:00~13:30，採 ROD 限價。
        """
        return self.place_order(
            symbol=symbol,
            action=action,
            quantity=shares,
            price=price,
            price_type=StockPriceType.LMT,
            order_type=OrderType.ROD,
            custom_field=custom_field,
            order_lot=StockOrderLot.IntradayOdd,
            max_sell_qty=max_sell_qty,
        )

    # ------------------------------------------------------------------
    # 歷史盤中資料 (分 K / Tick)
    # ------------------------------------------------------------------

    def fetch_kbars(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        timeout_ms: int = 60_000,
    ):
        """抓取 Shioaji 1 分 K 歷史 (start/end: YYYY-MM-DD)。"""
        assert self.api is not None, "尚未登入"
        contract = self.get_contract(symbol)
        if contract is None:
            return None
        return self.api.kbars(
            contract,
            start=start,
            end=end,
            timeout=timeout_ms,
        )

    def fetch_ticks(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        timeout_ms: int = 60_000,
    ):
        """抓取 Shioaji 逐筆成交歷史 (start/end: YYYY-MM-DD)。"""
        assert self.api is not None, "尚未登入"
        contract = self.get_contract(symbol)
        if contract is None:
            return None
        return self.api.ticks(
            contract,
            start=start,
            end=end,
            timeout=timeout_ms,
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

    def update_status(self, trade: Optional["Trade"] = None) -> None:
        assert self.api is not None
        if trade is not None:
            self.api.update_status(trade=trade)
        else:
            self.api.update_status(self.api.stock_account)

    def list_trades(self) -> list:
        assert self.api is not None
        return self.api.list_trades()

    def cancel_order(self, trade: "Trade") -> Optional["Trade"]:
        """Cancel an order and refresh status using Shioaji's recommended flow."""
        assert self.api is not None
        try:
            self.update_status(trade=trade)
        except Exception:
            self.logger.debug("cancel_order pre-refresh failed", exc_info=True)
        try:
            cancelled = self.api.cancel_order(trade)
        except Exception:
            self.logger.exception("撤單失敗")
            return None
        try:
            self.update_status(trade=cancelled or trade)
        except Exception:
            self.logger.debug("cancel_order post-refresh failed", exc_info=True)
        return cancelled


def _quote_type_label(quote_type: QuoteType) -> str:
    value = getattr(quote_type, "value", "")
    if value:
        return str(value)
    return "tick"


def _clean_custom_field(custom_field: str) -> str:
    return clean_order_field(custom_field)


def _lookup_stock_contract(api: sj.Shioaji, symbol: str) -> Optional["Contract"]:
    stocks = api.Contracts.Stocks
    for getter in (
        lambda: stocks.get(symbol),
        lambda: stocks.TSE.get(symbol),
        lambda: stocks.OTC.get(symbol),
        lambda: stocks[symbol],
    ):
        try:
            contract = getter()
        except Exception:
            continue
        if contract is not None:
            return contract
    return None

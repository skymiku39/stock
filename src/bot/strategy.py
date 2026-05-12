"""策略引擎 -- BaseStrategy 基底類別 + MyStrategy 示範當沖策略。

BaseStrategy 提供：
  - 部位管理 / 委託單追蹤
  - 行情回呼 → Queue 解耦 (trade/watch) 或輪詢迴圈 (report)
  - 收盤全出場定時器
  - 委託狀態輪詢更新器 (trade 模式)
  - watch/report 模式的虛擬部位 + SignalRecorder

MyStrategy 示範：
  - 開盤 N 分鐘內、漲幅 1%~5% 買進
  - 固定百分比停損 / 停利
  - 收盤前市價全出
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from queue import Empty, Queue
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from shioaji import Exchange, TickSTKv1
from shioaji.constant import Action, OrderState

from bot.models import MarketTick, OrderRecord, PositionInfo, SignalEvent
from bot.notifier import TelegramNotifier
from bot.recorder import TradeRecorder
from bot.signal_recorder import SignalRecorder
from bot.utils import get_logger, now_tw, now_tw_time

if TYPE_CHECKING:
    from bot.broker import SjBroker
    from bot.config import Settings
    from bot.market_source import TwsePublicMarketSource


class BaseStrategy(ABC):
    """策略基底類別。"""

    def __init__(
        self,
        broker: Optional[SjBroker],
        settings: Settings,
        market_source: Optional[TwsePublicMarketSource] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.broker = broker
        self.settings = settings
        self._market_source = market_source
        self.logger = logger or get_logger("strategy")

        # 部位管理
        self.positions: Dict[str, PositionInfo] = {}

        # 委託追蹤: symbol -> list of pending order_no (trade 模式)
        self.pending_orders: Dict[str, List[str]] = defaultdict(list)
        self._pending_lock: Dict[str, threading.Lock] = defaultdict(threading.Lock)

        # 已送出進場/出場的 order record
        self.order_records: Dict[str, OrderRecord] = {}

        # 已送出進場單的 symbol (避免重複進場)
        self._enter_placed: Set[str] = set()

        # 收盤全出場標記
        self._closure_placed: Set[str] = set()

        # Tick 佇列: (exchange, tick) -- trade/watch 模式
        self._tick_queue: Queue = Queue(maxsize=50_000)

        # 資金追蹤: 已投入的總金額
        self._fund_used: float = 0.0

        # 前日收盤 (所有模式共用)
        self._prev_close: Dict[str, float] = {}

        # 最新成交價追蹤 (虛擬出場用)
        self._last_price: Dict[str, float] = {}

        # 交易紀錄 (trade 模式)
        self.recorder = TradeRecorder()

        # 訊號紀錄 (watch/report 模式)
        self.signal_recorder = SignalRecorder(
            output_dir=settings.report_output_dir,
        )

        # 通知
        self.notifier = TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )

        # 控制旗標
        self._running = False

    # ------------------------------------------------------------------
    # 模式判斷
    # ------------------------------------------------------------------

    @property
    def _is_trade_mode(self) -> bool:
        return self.settings.run_mode == "trade"

    # ------------------------------------------------------------------
    # 啟動 / 停止
    # ------------------------------------------------------------------

    def run(self) -> None:
        """啟動策略 (阻塞主執行緒直到收盤或手動中斷)。"""
        self._running = True

        if self.settings.run_mode in ("trade", "watch"):
            self._run_shioaji_mode()
        else:
            self._run_report_mode()

    def _run_shioaji_mode(self) -> None:
        """trade / watch 模式: Shioaji 即時行情。"""
        assert self.broker is not None, "trade/watch 模式需要 SjBroker"

        self.broker.set_on_tick(self._enqueue_tick)
        if self._is_trade_mode:
            self.broker.set_on_order(self._on_order_callback)

        self._fetch_prev_close_shioaji()
        self._subscribe_symbols()

        threads: List[threading.Thread] = [
            threading.Thread(
                target=self._tick_consumer, daemon=True, name="tick-consumer",
            ),
            threading.Thread(
                target=self._position_closure, daemon=True, name="closure",
            ),
        ]
        if self._is_trade_mode:
            threads.append(
                threading.Thread(
                    target=self._order_status_updater,
                    daemon=True,
                    name="order-updater",
                )
            )
        for t in threads:
            t.start()

        mode_label = "交易" if self._is_trade_mode else "看盤"
        self.logger.info("策略已啟動 [%s 模式]，監控 %s", mode_label, self.settings.symbols)
        self.notifier.notify_start(
            self.settings.symbols, self.settings.simulation,
        )

        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("收到中斷訊號，準備停止 ...")
        finally:
            self.stop()

    def _run_report_mode(self) -> None:
        """report 模式: 公開延遲資料輪詢。"""
        assert self._market_source is not None, "report 模式需要 MarketSource"

        self._fetch_prev_close_public()

        closure_thread = threading.Thread(
            target=self._position_closure, daemon=True, name="closure",
        )
        closure_thread.start()

        self.logger.info(
            "策略已啟動 [報表模式]，輪詢間隔 %ds，監控 %s",
            self.settings.report_poll_seconds,
            self.settings.symbols,
        )

        try:
            while self._running:
                ticks = self._market_source.poll()
                for tick in ticks:
                    self._last_price[tick.symbol] = tick.price
                    try:
                        self.on_tick(tick)
                    except Exception:
                        self.logger.exception("on_tick 處理例外: %s", tick.symbol)
                time.sleep(self.settings.report_poll_seconds)
        except KeyboardInterrupt:
            self.logger.info("收到中斷訊號，準備停止 ...")
        finally:
            self.stop()

    def stop(self) -> None:
        self._running = False
        self.logger.info("策略已停止")

    # ------------------------------------------------------------------
    # 行情處理 (Queue 解耦, trade/watch)
    # ------------------------------------------------------------------

    def _enqueue_tick(self, exchange: Exchange, tick: TickSTKv1) -> None:
        try:
            self._tick_queue.put_nowait((exchange, tick))
        except Exception:
            pass

    def _tick_consumer(self) -> None:
        """獨立執行緒，將 Shioaji TickSTKv1 正規化為 MarketTick 後送入策略。"""
        while self._running:
            try:
                exchange, sj_tick = self._tick_queue.get(timeout=1)
            except Empty:
                continue

            tick = MarketTick(
                ts=getattr(sj_tick, "datetime", now_tw()),
                symbol=sj_tick.code,
                price=float(sj_tick.close),
                volume=int(getattr(sj_tick, "volume", 0)),
                pct_chg=float(sj_tick.pct_chg) if sj_tick.pct_chg else 0.0,
                prev_close=self._prev_close.get(sj_tick.code, 0.0),
                source="shioaji",
            )
            self._last_price[tick.symbol] = tick.price

            try:
                self.on_tick(tick)
            except Exception:
                self.logger.exception("on_tick 處理例外: %s", tick.symbol)

    # ------------------------------------------------------------------
    # 盤前準備
    # ------------------------------------------------------------------

    def _fetch_prev_close_shioaji(self) -> None:
        assert self.broker is not None
        refs = self.broker.get_snapshots(self.settings.symbols)
        self._prev_close.update(refs)
        self._on_prev_close_ready(refs)

    def _fetch_prev_close_public(self) -> None:
        assert self._market_source is not None
        refs = self._market_source.get_prev_close(self.settings.symbols)
        self._prev_close.update(refs)
        self._on_prev_close_ready(refs)

    def _on_prev_close_ready(self, refs: Dict[str, float]) -> None:
        """子類別可覆寫以接收前日收盤價。預設不做事。"""

    # ------------------------------------------------------------------
    # 訂閱 (trade/watch)
    # ------------------------------------------------------------------

    def _subscribe_symbols(self) -> None:
        assert self.broker is not None
        for symbol in self.settings.symbols:
            self.broker.subscribe_tick(symbol)
            time.sleep(0.1)

    # ------------------------------------------------------------------
    # 委託回報處理 (trade 模式)
    # ------------------------------------------------------------------

    def _on_order_callback(self, stat: OrderState, msg: dict) -> None:
        try:
            if stat == OrderState.StockDeal:
                self._handle_deal(msg)
            elif stat == OrderState.StockOrder:
                self._handle_order_event(msg)
        except Exception:
            self.logger.exception("委託回報處理例外: stat=%s", stat)

    def _handle_order_event(self, msg: dict) -> None:
        op = msg.get("operation", {})
        order = msg.get("order", {})
        op_type = op.get("op_type", "")
        op_code = op.get("op_code", "")
        symbol = msg.get("contract", {}).get("code", "")

        if op_code != "00":
            self.logger.warning(
                "委託失敗: %s %s op=%s msg=%s",
                symbol, op_type, op_code, op.get("op_msg", ""),
            )

    def _handle_deal(self, msg: dict) -> None:
        """處理成交回報，更新部位並推入交易紀錄 (trade 模式)。"""
        self.recorder.record_deal(msg)

        symbol = msg.get("code", "")
        action = msg.get("action", "")
        price = float(msg.get("price", 0))
        qty = int(msg.get("quantity", 0))
        custom = msg.get("custom_field", "")

        self.logger.info(
            "成交回報: %s %s %d 張 @ %.2f [%s]",
            action, symbol, qty, price, custom,
        )

        with self._pending_lock[symbol]:
            ordno = msg.get("ordno", "")
            if ordno in self.pending_orders.get(symbol, []):
                self.pending_orders[symbol].remove(ordno)

            if action == "Buy":
                cost = price * qty * 1000
                self._fund_used += cost
                if symbol in self.positions:
                    self.positions[symbol].update(price, qty)
                else:
                    self.positions[symbol] = PositionInfo(
                        symbol=symbol, avg_price=price, quantity=qty,
                    )
                self.logger.info(
                    "部位更新 (買入): %s | 已用資金: %.0f",
                    self.positions[symbol], self._fund_used,
                )
                self.notifier.notify_buy(symbol, price, qty)

            elif action == "Sell":
                released = price * qty * 1000
                self._fund_used = max(0.0, self._fund_used - released)
                self.notifier.notify_sell(symbol, price, qty, custom)
                if symbol in self.positions:
                    closed = self.positions[symbol].reduce(qty)
                    if closed:
                        del self.positions[symbol]
                        self.logger.info("部位已清空: %s", symbol)
                    else:
                        self.logger.info("部位更新 (賣出): %s", self.positions[symbol])

    # ------------------------------------------------------------------
    # 虛擬成交 (watch / report)
    # ------------------------------------------------------------------

    def _virtual_fill_buy(
        self, symbol: str, price: float, quantity: int, reason: str,
    ) -> None:
        cost = price * quantity * 1000
        self._fund_used += cost

        if symbol in self.positions:
            self.positions[symbol].update(price, quantity)
        else:
            self.positions[symbol] = PositionInfo(
                symbol=symbol, avg_price=price, quantity=quantity,
            )

        prev_close = self._prev_close.get(symbol, 0.0)
        pct_chg = (
            100 * (price - prev_close) / prev_close if prev_close > 0 else 0.0
        )

        self.signal_recorder.record(SignalEvent(
            ts=now_tw(),
            symbol=symbol,
            action="would-buy",
            price=price,
            quantity=quantity,
            reason=reason,
            pct_chg=pct_chg,
            pnl_pct=0.0,
            mode=self.settings.run_mode,
            source=self.settings.market_source,
        ))
        self.logger.info(
            "[虛擬買入] %s %.2f x %d [%s]", symbol, price, quantity, reason,
        )

    def _virtual_fill_sell(
        self, symbol: str, price: float, quantity: int, reason: str,
    ) -> None:
        released = price * quantity * 1000
        self._fund_used = max(0.0, self._fund_used - released)

        pnl_pct = 0.0
        if symbol in self.positions:
            pos = self.positions[symbol]
            if pos.avg_price > 0:
                pnl_pct = 100 * (price - pos.avg_price) / pos.avg_price
            closed = pos.reduce(quantity)
            if closed:
                del self.positions[symbol]

        prev_close = self._prev_close.get(symbol, 0.0)
        pct_chg = (
            100 * (price - prev_close) / prev_close if prev_close > 0 else 0.0
        )

        self.signal_recorder.record(SignalEvent(
            ts=now_tw(),
            symbol=symbol,
            action="would-sell",
            price=price,
            quantity=quantity,
            reason=reason,
            pct_chg=pct_chg,
            pnl_pct=pnl_pct,
            mode=self.settings.run_mode,
            source=self.settings.market_source,
        ))
        self.logger.info(
            "[虛擬賣出] %s %.2f x %d [%s] PnL=%.2f%%",
            symbol, price, quantity, reason, pnl_pct,
        )

    # ------------------------------------------------------------------
    # 委託狀態輪詢 (trade 模式)
    # ------------------------------------------------------------------

    def _order_status_updater(self) -> None:
        """定期輪詢委託狀態，清理已完成的 pending orders。"""
        assert self.broker is not None
        while self._running and now_tw_time() < self.settings.exit_time:
            has_pending = any(len(v) > 0 for v in self.pending_orders.values())
            if not has_pending:
                time.sleep(2)
                continue

            try:
                self.broker.update_status()
                for trade in self.broker.list_trades():
                    status = trade.status.status.value
                    ordno = getattr(trade.order, "ordno", "")
                    symbol = getattr(trade.contract, "code", "")

                    if status in ("Filled", "Cancelled", "Failed"):
                        with self._pending_lock[symbol]:
                            if ordno in self.pending_orders.get(symbol, []):
                                self.pending_orders[symbol].remove(ordno)
            except Exception:
                self.logger.exception("委託狀態更新例外")

            time.sleep(3)

    # ------------------------------------------------------------------
    # 收盤全出場
    # ------------------------------------------------------------------

    def _position_closure(self) -> None:
        """在 exit_time 之後，將所有部位市價全出 (或虛擬出清)。"""
        while self._running:
            cur = now_tw_time()
            if cur < self.settings.exit_time:
                time.sleep(1)
                continue

            for symbol, pos in list(self.positions.items()):
                if symbol in self._closure_placed:
                    continue

                if pos.quantity <= 0:
                    continue

                if self._is_trade_mode:
                    with self._pending_lock[symbol]:
                        if len(self.pending_orders.get(symbol, [])) > 0:
                            continue

                        self.logger.info(
                            "全出場: %s %d 張 (市價 IOC)", symbol, pos.quantity,
                        )
                        assert self.broker is not None
                        trade = self.broker.place_market_sell(
                            symbol, pos.quantity, custom_field="close",
                        )
                        if trade:
                            ordno = getattr(trade.order, "ordno", "")
                            self.pending_orders[symbol].append(ordno)
                            self._closure_placed.add(symbol)
                else:
                    price = self._last_price.get(symbol, pos.avg_price)
                    self.logger.info(
                        "[虛擬全出場] %s %d 張 @ %.2f", symbol, pos.quantity, price,
                    )
                    self._virtual_fill_sell(symbol, price, pos.quantity, "close")
                    self._closure_placed.add(symbol)

            if not self.positions:
                self.logger.info("所有部位已清空，策略結束")
                if self._is_trade_mode:
                    self.notifier.notify_closure(self.recorder.summary())
                self._running = False
                break

            time.sleep(1)

    # ------------------------------------------------------------------
    # 下單輔助
    # ------------------------------------------------------------------

    def _has_pending(self, symbol: str) -> bool:
        if not self._is_trade_mode:
            return False
        with self._pending_lock[symbol]:
            return len(self.pending_orders.get(symbol, [])) > 0

    def _place_buy(
        self,
        symbol: str,
        price: float,
        quantity: int,
        custom_field: str = "enter",
    ) -> bool:
        if not self._is_trade_mode:
            self._virtual_fill_buy(symbol, price, quantity, custom_field)
            self._enter_placed.add(symbol)
            return True

        assert self.broker is not None
        trade = self.broker.place_order(
            symbol=symbol,
            action=Action.Buy,
            quantity=quantity,
            price=price,
            custom_field=custom_field,
        )
        if trade is None:
            return False

        ordno = getattr(trade.order, "ordno", "")
        with self._pending_lock[symbol]:
            self.pending_orders[symbol].append(ordno)
        self._enter_placed.add(symbol)
        return True

    def _place_stop_sell(
        self,
        symbol: str,
        quantity: int,
        custom_field: str = "stop",
    ) -> bool:
        if not self._is_trade_mode:
            price = self._last_price.get(symbol, 0.0)
            self._virtual_fill_sell(symbol, price, quantity, custom_field)
            return True

        assert self.broker is not None
        trade = self.broker.place_market_sell(symbol, quantity, custom_field)
        if trade is None:
            return False

        ordno = getattr(trade.order, "ordno", "")
        with self._pending_lock[symbol]:
            self.pending_orders[symbol].append(ordno)
        return True

    # ------------------------------------------------------------------
    # 抽象方法
    # ------------------------------------------------------------------

    @abstractmethod
    def on_tick(self, tick: MarketTick) -> None:
        """收到正規化 Tick 時的策略邏輯，由子類別實作。"""
        ...


# ======================================================================
# 示範策略
# ======================================================================


class MyStrategy(BaseStrategy):
    """簡單當沖示範策略。

    進場: 開盤後至 enter_cutoff_time 前，漲幅 1%~5% 時限價買進。
    出場: 停損 / 停利 百分比觸發 → 市價 IOC 賣出。
    全出場: exit_time 後市價清倉。
    """

    def __init__(
        self,
        broker: Optional[SjBroker],
        settings: Settings,
        market_source: Optional[TwsePublicMarketSource] = None,
        logger: Optional[logging.Logger] = None,
    ):
        super().__init__(broker, settings, market_source, logger)
        self._high_watermark: Dict[str, float] = {}

    def _on_prev_close_ready(self, refs: Dict[str, float]) -> None:
        self.logger.info("前日收盤已載入: %s", refs)

    def on_tick(self, tick: MarketTick) -> None:
        symbol = tick.symbol
        price = tick.price
        cur_time = now_tw_time()

        if symbol not in self._prev_close:
            if tick.pct_chg:
                ref = price / (1 + tick.pct_chg / 100)
            else:
                ref = price
            self._prev_close[symbol] = ref
            self.logger.debug("反推前日收盤: %s = %.2f", symbol, ref)

        prev_close = self._prev_close[symbol]
        if prev_close <= 0:
            return

        pct_chg = 100 * (price - prev_close) / prev_close

        # === 進場邏輯 ===
        if (
            cur_time < self.settings.enter_cutoff_time
            and symbol not in self._enter_placed
            and not self._has_pending(symbol)
            and symbol not in self.positions
        ):
            if 1.0 < pct_chg < 5.0:
                lots = self._calc_lots(price)
                if lots > 0:
                    self.logger.info(
                        "[進場] %s 漲幅 %.2f%% price=%.2f lots=%d",
                        symbol, pct_chg, price, lots,
                    )
                    self._place_buy(symbol, price, lots, custom_field="enter")

        # === 停損 / 停利邏輯 ===
        if (
            cur_time < self.settings.exit_time
            and symbol in self.positions
            and not self._has_pending(symbol)
        ):
            pos = self.positions[symbol]
            pnl_pct = 100 * (price - pos.avg_price) / pos.avg_price

            hw = self._high_watermark.get(symbol, price)
            if price > hw:
                self._high_watermark[symbol] = price
                hw = price

            drawdown_pct = 100 * (hw - price) / hw if hw > 0 else 0

            if (
                pnl_pct >= self.settings.take_profit_pct
                and drawdown_pct >= self.settings.trailing_stop_pct
            ):
                self.logger.info(
                    "[移動停利] %s PnL=%.2f%% 高點=%.2f 回撤=%.2f%%",
                    symbol, pnl_pct, hw, drawdown_pct,
                )
                self._place_stop_sell(symbol, pos.quantity, custom_field="trail")

            elif pnl_pct <= self.settings.stop_loss_pct:
                self.logger.info(
                    "[停損] %s PnL=%.2f%% (<= %.1f%%)",
                    symbol, pnl_pct, self.settings.stop_loss_pct,
                )
                self._place_stop_sell(symbol, pos.quantity, custom_field="sl")

    def _calc_lots(self, price: float) -> int:
        """根據剩餘可用資金與每檔最大張數計算可買張數。"""
        cost_per_lot = price * 1000
        if cost_per_lot <= 0:
            return 0

        remaining = self.settings.max_fund - self._fund_used
        if remaining < cost_per_lot:
            self.logger.debug(
                "資金不足: 剩餘 %.0f < 每張 %.0f", remaining, cost_per_lot,
            )
            return 0

        max_by_fund = int(remaining / cost_per_lot)
        return min(max_by_fund, self.settings.max_lot_per_symbol)

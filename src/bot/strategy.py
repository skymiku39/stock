"""策略引擎 -- BaseStrategy 基底類別。

BaseStrategy 提供：
  - 部位管理 / 委託單追蹤
  - 行情回呼 → Queue 解耦 (trade/watch) 或輪詢迴圈 (report)
  - 收盤全出場定時器
  - 委託狀態輪詢更新器 (trade 模式)
  - watch/report 模式的虛擬部位 + SignalRecorder
  - 共用出場邏輯（使用者目標 / 移動停利 / 停損）

具體進場邏輯由子類別實作：`ConfigurableStrategy`、`EtfFollowStrategy`。
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import TYPE_CHECKING

from shioaji import Exchange, TickSTKv1
from shioaji.constant import Action, OrderState

from bot.events import (
    BotStarted,
    ClosureCompleted,
    RiskEntryBlocked,
    TickReceived,
    TradeBuyFilled,
    TradeSellFilled,
    get_event_bus,
    publish_if_bus,
    wire_trading_handlers,
)
from bot.events.protocols import EventPublisher
from bot.intraday_llm_advisor import IntradayLlmAdvisor
from bot.llm_gate import LlmGate
from bot.models import (
    MarketTick,
    OrderRecord,
    PositionInfo,
    QtyUnit,
    SignalEvent,
)
from bot.notifier import TelegramNotifier
from bot.ownership import (
    BOT_OWNER_TAG,
    bot_buy_field,
    bot_sell_field,
    is_bot_order_field,
    is_bot_owner,
)
from bot.recorder import TradeRecorder
from bot.risk_guard import EntryKind, RiskGuard
from bot.signal_recorder import SignalRecorder
from bot.trade_cost import (
    buy_cash_required,
    max_affordable_qty,
    net_pnl_twd,
    position_net_pnl_pct,
    rebuy_opportunity,
)
from bot.utils import get_logger, now_tw, now_tw_time

if TYPE_CHECKING:
    from bot.broker import SjBroker
    from bot.config import Settings
    from bot.market_source import TwsePublicMarketSource


@dataclass
class SymbolExitRecord:
    """單檔最近一次賣出紀錄（供回落買回）。"""
    exit_price: float
    exit_net_pnl_pct: float
    exit_time: dt.datetime
    quantity: int
    unit: QtyUnit


class BaseStrategy(ABC):
    """策略基底類別。"""

    def __init__(
        self,
        broker: SjBroker | None,
        settings: Settings,
        market_source: TwsePublicMarketSource | None = None,
        logger: logging.Logger | None = None,
        publisher: EventPublisher | None = None,
        *,
        wire_handlers: bool = True,
    ):
        self.broker = broker
        self.settings = settings
        self._market_source = market_source
        self.logger = logger or get_logger("strategy")
        self._publisher: EventPublisher = publisher or get_event_bus()

        # 部位管理
        self.positions: dict[str, PositionInfo] = {}

        # 委託追蹤: symbol -> list of pending order_no (trade 模式)
        self.pending_orders: dict[str, list[str]] = defaultdict(list)
        self._pending_lock: dict[str, threading.Lock] = defaultdict(threading.Lock)

        # 已送出進場/出場的 order record
        self.order_records: dict[str, OrderRecord] = {}

        # 已送出進場單的 symbol (避免重複送單；平倉後清除)
        self._enter_placed: set[str] = set()

        # 單檔最近一次賣出（回落買回用）
        self._symbol_exits: dict[str, SymbolExitRecord] = {}

        # 持倉期間淨利高點 %（移動停利用）
        self._peak_net_pnl: dict[str, float] = {}

        # 收盤全出場標記
        self._closure_placed: set[str] = set()
        self._closure_pending: set[str] = set()
        self._closure_blocked: set[str] = set()

        # 委託 metadata: ordno -> {symbol, action, custom_field}
        self._order_meta: dict[str, dict] = {}

        # Tick 佇列: (exchange, tick) -- trade/watch 模式
        self._tick_queue: Queue = Queue(maxsize=50_000)

        # 進場單位追蹤 (整張 / 零股)
        self._entry_units: dict[str, QtyUnit] = {}

        # 前日收盤 (所有模式共用)
        self._prev_close: dict[str, float] = {}

        # 最新成交價追蹤 (虛擬出場用)
        self._last_price: dict[str, float] = {}

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

        # 風險 / 資金控制 (所有實際下單都會經過)
        self.risk = RiskGuard(settings=settings, logger=self.logger)
        self._fund_used = self.risk.fund_used

        # LLM 進出場閘門 (賣出前 AI 分析等)
        self.llm_gate = LlmGate(
            settings=settings,
            risk=self.risk,
            logger=self.logger,
        )
        self.intraday_advisor = IntradayLlmAdvisor(
            settings,
            project_root=Path.cwd(),
            risk=self.risk,
            llm_gate=self.llm_gate,
            logger=self.logger,
        )

        if wire_handlers:
            wire_trading_handlers(
                self._publisher,
                recorder=self.recorder,
                notifier=self.notifier,
                risk=self.risk,
            )

        # 控制旗標
        self._running = False

        # 四源監控池
        self._watch_pool_lock = threading.Lock()
        self._last_watch_pool_refresh = 0.0
        self._watch_subscribed: set[str] = set()

    # ------------------------------------------------------------------
    # 模式判斷
    # ------------------------------------------------------------------

    @property
    def _is_trade_mode(self) -> bool:
        return self.settings.run_mode == "trade"

    def _fund_cap(self) -> int:
        return self.settings.effective_fund_cap()

    def _run_startup_safety_and_restore(self) -> None:
        """啟動對帳 + 恢復持久化資金與 AI 部位。"""
        from bot.portfolio import load_bot_portfolio
        from bot.position_safety import run_startup_safety_checks

        report = run_startup_safety_checks(
            self.settings,
            project_root=Path.cwd(),
            broker=self.broker,
            risk=self.risk,
        )
        if not report.ok:
            for msg in report.blocking_messages:
                self.logger.warning("啟動安全檢查: %s", msg)
            self.logger.warning(
                "啟動安全檢查未通過 (%s)，Kill Switch 已拉起，僅允許平倉既有 AI 部位",
                report.summary,
            )
        else:
            self.logger.info("啟動安全檢查通過")

        self._fund_used = self.risk.fund_used
        _, bot_positions = load_bot_portfolio(Path.cwd())
        monitored = set(self.settings.symbols or [])
        for symbol, ppos in bot_positions.items():
            if symbol not in monitored or ppos.qty <= 0:
                continue
            if ppos.qty >= 1.0 - 1e-9:
                unit: QtyUnit = "lot"
                quantity = int(round(ppos.qty))
            elif self.settings.use_odd_lot:
                unit = "share"
                quantity = int(round(ppos.qty * 1000))
            else:
                continue
            if quantity <= 0:
                continue
            self.positions[symbol] = PositionInfo(
                symbol=symbol,
                avg_price=ppos.avg_cost,
                quantity=quantity,
                owner_tag=BOT_OWNER_TAG,
                unit=unit,
            )
            self._enter_placed.add(symbol)
            self.logger.info(
                "恢復 AI 部位: %s %d %s @ %.2f",
                symbol,
                quantity,
                "股" if unit == "share" else "張",
                ppos.avg_cost,
            )

        if self._fund_used <= 0 and self.positions:
            restored_cost = sum(
                buy_cash_required(
                    pos.avg_price, pos.quantity, pos.unit, settings=self.settings,
                )
                for pos in self.positions.values()
            )
            if restored_cost > 0:
                self._fund_used = restored_cost
                self.risk.set_fund_used(
                    restored_cost,
                    open_symbols=list(self.positions.keys()),
                )
                self.logger.info("由部位推算已用資金: %.0f", self._fund_used)

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

        if getattr(self.settings, "symbols_auto_merge", True):
            self._merge_watch_pool(reason="startup", refresh_live_quotes=False)

        if self._is_trade_mode:
            self._run_startup_safety_and_restore()
            self._ensure_position_symbols_monitored()

        self.broker.set_on_tick(self._enqueue_tick)
        if self._is_trade_mode:
            self.broker.set_on_order(self._on_order_callback)

        self._fetch_prev_close_shioaji()
        self._subscribe_symbols()

        threads: list[threading.Thread] = [
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
        if getattr(self.settings, "llm_intraday_review_enabled", False):
            threads.append(
                threading.Thread(
                    target=lambda: self.intraday_advisor.run_loop(
                        lambda: self._running,
                    ),
                    daemon=True,
                    name="intraday-llm-advisor",
                )
            )
        if getattr(self.settings, "symbols_auto_merge", True):
            threads.append(
                threading.Thread(
                    target=self._watch_pool_refresh_loop,
                    daemon=True,
                    name="watch-pool-refresh",
                )
            )
        for t in threads:
            t.start()

        mode_label = "交易" if self._is_trade_mode else "看盤"
        self.logger.info("策略已啟動 [%s 模式]，監控 %s", mode_label, self.settings.symbols)
        publish_if_bus(
            self._publisher,
            BotStarted(
                symbols=tuple(self.settings.symbols),
                run_mode=self.settings.run_mode,
                simulation=self.settings.simulation,
            ),
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
                    self.signal_recorder.record_tick(tick)
                    publish_if_bus(
                        self._publisher,
                        TickReceived(
                            symbol=tick.symbol,
                            price=tick.price,
                            pct_chg=tick.pct_chg,
                            source=tick.source,
                        ),
                    )
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
            if not self._is_trade_mode:
                self.signal_recorder.record_tick(tick)

            publish_if_bus(
                self._publisher,
                TickReceived(
                    symbol=tick.symbol,
                    price=tick.price,
                    pct_chg=tick.pct_chg,
                    source=tick.source,
                ),
            )

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

    def _on_prev_close_ready(self, refs: dict[str, float]) -> None:
        """子類別可覆寫以接收前日收盤價。預設不做事。"""

    # ------------------------------------------------------------------
    # 訂閱 (trade/watch)
    # ------------------------------------------------------------------

    def _subscribe_symbols(self) -> None:
        assert self.broker is not None
        for symbol in self.settings.symbols:
            if symbol in self._watch_subscribed:
                continue
            self.broker.subscribe_tick(symbol)
            self._watch_subscribed.add(symbol)
            time.sleep(0.1)

    def _ensure_position_symbols_monitored(self) -> None:
        """持倉標的永遠留在監控池（即使已跌出四源排行）。"""
        symbols = list(self.settings.symbols or [])
        changed = False
        for symbol in self.positions:
            if symbol not in symbols:
                symbols.append(symbol)
                changed = True
        if changed:
            self.settings.symbols = symbols

    def _merge_watch_pool(
        self,
        *,
        reason: str,
        refresh_live_quotes: bool = False,
    ) -> list[str]:
        from bot.watch_symbol_pool import merge_watch_symbols_into_settings

        if not getattr(self.settings, "symbols_auto_merge", True):
            return []
        with self._watch_pool_lock:
            before = set(self.settings.symbols or [])
            merge_watch_symbols_into_settings(
                self.settings,
                Path.cwd(),
                reason=reason,
                refresh_live_quotes=refresh_live_quotes,
                logger=self.logger,
            )
            self._ensure_position_symbols_monitored()
            return [s for s in self.settings.symbols if s not in before]

    def _sync_watch_subscriptions(self, added: list[str]) -> None:
        if not added or self.broker is None:
            return
        need = [s for s in added if s not in self._watch_subscribed]
        if not need:
            return
        refs = self.broker.get_snapshots(need)
        self._prev_close.update(refs)
        for symbol in need:
            self.broker.subscribe_tick(symbol)
            self._watch_subscribed.add(symbol)
            time.sleep(0.1)

    def _watch_pool_refresh_loop(self) -> None:
        from bot.watch_symbol_pool import in_watch_pool_refresh_window

        tick_sec = 60
        while self._running:
            time.sleep(tick_sec)
            if not getattr(self.settings, "symbols_auto_merge", True):
                continue
            if not in_watch_pool_refresh_window():
                continue
            interval_sec = max(
                60,
                int(getattr(self.settings, "symbols_merge_refresh_min", 10)) * 60,
            )
            if time.time() - self._last_watch_pool_refresh < interval_sec:
                continue
            try:
                added = self._merge_watch_pool(
                    reason="scheduled",
                    refresh_live_quotes=True,
                )
                self._sync_watch_subscriptions(added)
                self._last_watch_pool_refresh = time.time()
            except Exception:
                self.logger.exception("監控池盤中刷新失敗")

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
            was_pending = ordno in self.pending_orders.get(symbol, [])
            if was_pending:
                self.pending_orders[symbol].remove(ordno)

            if action == "Buy":
                if not (was_pending or is_bot_order_field(custom)):
                    self.logger.info(
                        "忽略非 AI 標籤買進成交: %s %d 張 @ %.2f [%s]",
                        symbol, qty, price, custom,
                    )
                    return
                unit = self._entry_units.pop(symbol, "lot")
                buy_meta = self._order_meta.pop(ordno, {})
                trade_reason = str(buy_meta.get("custom_field", "enter"))
                if symbol in self.positions:
                    unit = self.positions[symbol].unit
                if symbol in self.positions:
                    self.positions[symbol].update(price, qty)
                else:
                    self.positions[symbol] = PositionInfo(
                        symbol=symbol, avg_price=price, quantity=qty,
                        owner_tag=BOT_OWNER_TAG, unit=unit,
                    )
                self.risk.on_entry_filled(symbol, price, qty, unit=unit)
                self._fund_used = self.risk.fund_used
                publish_if_bus(
                    self._publisher,
                    TradeBuyFilled(
                        symbol=symbol,
                        price=price,
                        quantity=qty,
                        unit=unit,
                        order_msg=dict(msg),
                        trade_reason=trade_reason,
                    ),
                )
                self.logger.info(
                    "部位更新 (買入): %s | 已用資金: %.0f",
                    self.positions[symbol], self._fund_used,
                )
                self.intraday_advisor.request_refresh(f"fill:buy:{symbol}")

            elif action == "Sell":
                if symbol not in self.positions:
                    self.logger.info(
                        "忽略非 AI 持倉賣出成交: %s %d 張 @ %.2f [%s]",
                        symbol, qty, price, custom,
                    )
                    return
                sell_unit = self.positions[symbol].unit
                sell_meta = self._order_meta.pop(ordno, {})
                entry_price = self.positions[symbol].avg_price
                sell_pnl_pct = self._position_pnl_pct(symbol, price) or 0.0
                sell_pnl_twd = net_pnl_twd(
                    entry_price, price, qty, sell_unit,
                    settings=self.settings,
                )
                trade_reason = str(
                    sell_meta.get("custom_field", custom) or custom,
                )
                if not is_bot_owner(self.positions[symbol].owner_tag):
                    self.logger.warning(
                        "阻擋非 AI 標籤部位賣出更新: %s owner=%s",
                        symbol, self.positions[symbol].owner_tag,
                    )
                    return
                unit = self.positions[symbol].unit
                if symbol in self.positions:
                    closed = self.positions[symbol].reduce(qty)
                    self.risk.on_exit_filled(
                        symbol, entry_price, price, qty, unit=unit,
                        position_closed=closed,
                    )
                    self._fund_used = self.risk.fund_used
                    publish_if_bus(
                        self._publisher,
                        TradeSellFilled(
                            symbol=symbol,
                            price=price,
                            quantity=qty,
                            unit=unit,
                            order_msg=dict(msg),
                            trade_reason=trade_reason,
                            entry_price=entry_price,
                            pnl_pct=sell_pnl_pct,
                            pnl_twd=sell_pnl_twd,
                            position_closed=closed,
                        ),
                    )
                    self.intraday_advisor.request_refresh(f"fill:sell:{symbol}")
                    if closed:
                        self._on_position_closed(
                            symbol, entry_price, price, qty, unit,
                        )
                        del self.positions[symbol]
                        self._closure_pending.discard(symbol)
                        if custom == bot_sell_field("close"):
                            self._closure_placed.add(symbol)
                        self.logger.info("部位已清空: %s", symbol)
                    else:
                        self.logger.info("部位更新 (賣出): %s", self.positions[symbol])

    # ------------------------------------------------------------------
    # 虛擬成交 (watch / report)
    # ------------------------------------------------------------------

    def _virtual_fill_buy(
        self,
        symbol: str,
        price: float,
        quantity: int,
        reason: str,
        *,
        unit: QtyUnit = "lot",
        llm_gate: str = "",
    ) -> None:
        if symbol in self.positions:
            self.positions[symbol].update(price, quantity)
        else:
            self.positions[symbol] = PositionInfo(
                symbol=symbol, avg_price=price, quantity=quantity,
                owner_tag=BOT_OWNER_TAG, unit=unit,
            )
        self.risk.on_entry_filled(symbol, price, quantity, unit=unit)
        self._fund_used = self.risk.fund_used

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
            unit=unit,
            llm_gate=llm_gate,
        ))
        unit_label = "股" if unit == "share" else "張"
        self.logger.info(
            "[虛擬買入] %s %.2f x %d %s [%s]", symbol, price, quantity, unit_label, reason,
        )

    def _virtual_fill_sell(
        self,
        symbol: str,
        price: float,
        quantity: int,
        reason: str,
        *,
        unit: QtyUnit | None = None,
        llm_gate: str = "",
    ) -> None:
        pos = self.positions.get(symbol)
        sell_unit = unit or (pos.unit if pos else "lot")

        pnl_pct = 0.0
        entry_price = price
        closed = False
        if pos is not None:
            if pos.avg_price > 0:
                pnl_pct = self._position_pnl_pct(symbol, price) or 0.0
            entry_price = pos.avg_price
            closed = pos.reduce(quantity)
            if closed:
                self._on_position_closed(
                    symbol, entry_price, price, quantity, sell_unit,
                )
                del self.positions[symbol]
                self._peak_net_pnl.pop(symbol, None)
        self.risk.on_exit_filled(
            symbol, entry_price, price, quantity, unit=sell_unit,
            position_closed=closed,
        )
        self._fund_used = self.risk.fund_used
        if closed and reason == "close":
            self._closure_placed.add(symbol)

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
            unit=sell_unit,
            llm_gate=llm_gate,
        ))
        unit_label = "股" if sell_unit == "share" else "張"
        self.logger.info(
            "[虛擬賣出] %s %.2f x %d %s [%s] PnL=%.2f%%",
            symbol, price, quantity, unit_label, reason, pnl_pct,
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
                    action = str(getattr(trade.order, "action", ""))

                    if status in ("Filled", "Cancelled", "Failed"):
                        with self._pending_lock[symbol]:
                            if ordno in self.pending_orders.get(symbol, []):
                                self.pending_orders[symbol].remove(ordno)
                        if status in ("Cancelled", "Failed"):
                            meta = self._order_meta.pop(ordno, {})
                            meta_symbol = meta.get("symbol", symbol)
                            meta_action = meta.get("action", action)
                            if meta_action == "Buy" and meta_symbol not in self.positions:
                                self._enter_placed.discard(meta_symbol)
                                self.risk.release_entry_exposure(meta_symbol)
                            elif meta_action == "Sell":
                                if meta.get("custom_field") == "close":
                                    self._closure_pending.discard(meta_symbol)
                        elif status == "Filled":
                            self._order_meta.pop(ordno, None)
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
                if symbol in self._closure_placed or symbol in self._closure_pending:
                    continue

                if pos.quantity <= 0:
                    continue

                price = self._last_price.get(symbol, pos.avg_price)
                if not self._can_auto_sell(
                    symbol, pos.quantity, "close", price=price,
                ):
                    self._closure_blocked.add(symbol)
                    continue

                if self._is_trade_mode:
                    with self._pending_lock[symbol]:
                        if len(self.pending_orders.get(symbol, [])) > 0:
                            continue

                    self.logger.info(
                        "全出場: %s %d 張 (市價 IOC)", symbol, pos.quantity,
                    )
                    self._place_stop_sell(
                        symbol, pos.quantity, custom_field="close",
                    )
                else:
                    self.logger.info(
                        "[虛擬全出場] %s %d 張 @ %.2f", symbol, pos.quantity, price,
                    )
                    self._place_stop_sell(
                        symbol, pos.quantity, custom_field="close",
                    )

            if not self.positions:
                self.logger.info("所有部位已清空，策略結束")
                if self._is_trade_mode:
                    publish_if_bus(
                        self._publisher,
                        ClosureCompleted(trade_summary=self.recorder.summary()),
                    )
                self._running = False
                break

            if set(self.positions) <= self._closure_blocked:
                self.logger.warning(
                    "收盤出場被賣出授權限制阻擋，保留部位不自動賣出: %s",
                    sorted(self.positions),
                )
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

    def _calc_quantity(self, price: float) -> tuple[int, QtyUnit]:
        """依剩餘資金計算可買張數或零股數（含手續費）。"""
        if price <= 0:
            return 0, "lot"

        remaining = self.risk.effective_fund_remaining()
        qty = max_affordable_qty(
            price, remaining, "lot",
            max_qty=self.settings.max_lot_per_symbol,
            settings=self.settings,
        )
        if qty >= 1:
            return qty, "lot"

        if getattr(self.settings, "use_odd_lot", False):
            max_shares = max_affordable_qty(
                price, remaining, "share",
                max_qty=getattr(self.settings, "odd_lot_max_shares", 999),
                settings=self.settings,
            )
            if max_shares >= 1:
                return max_shares, "share"

        return 0, "lot"

    def _place_buy(
        self,
        symbol: str,
        price: float,
        quantity: int,
        custom_field: str = "enter",
        *,
        pct_chg: float | None = None,
        unit: QtyUnit = "lot",
        llm_gate: str = "",
    ) -> bool:
        # === 風控守門員 ===
        available_balance = None
        enforce_account_balance = False
        if self._is_trade_mode and self.settings.check_account_balance and self.broker:
            enforce_account_balance = True
            available_balance = self.broker.get_available_balance()

        if custom_field == "rebuy":
            entry_kind: EntryKind = "rebuy"
        elif custom_field == "reenter":
            entry_kind = "reenter"
        else:
            entry_kind = "enter"
        decision = self.risk.check_entry(
            symbol=symbol,
            price=price,
            requested_lots=quantity,
            unit=unit,
            pct_chg=pct_chg,
            available_balance=available_balance,
            enforce_account_balance=enforce_account_balance,
            entry_kind=entry_kind,
        )
        if not decision.allowed:
            self.logger.warning(
                "進場被風控拒絕 [%s] %s: %s",
                decision.blocking_rule, symbol, decision.reason,
            )
            publish_if_bus(
                self._publisher,
                RiskEntryBlocked(
                    symbol=symbol,
                    reason=decision.reason,
                    blocking_rule=decision.blocking_rule,
                ),
            )
            return False
        if decision.adjusted_lots != quantity:
            unit_label = "股" if decision.unit == "share" else "張"
            self.logger.info(
                "數量經風控調整: %s %d → %d %s",
                symbol, quantity, decision.adjusted_lots, unit_label,
            )
        quantity = decision.adjusted_lots
        unit = decision.unit

        if not self._is_trade_mode:
            self._virtual_fill_buy(
                symbol, price, quantity, custom_field,
                unit=unit, llm_gate=llm_gate,
            )
            self._enter_placed.add(symbol)
            return True

        assert self.broker is not None
        reserved_cost = buy_cash_required(
            price, quantity, unit, settings=self.settings,
        )
        self.risk.reserve_entry_exposure(symbol, reserved_cost)
        self._entry_units[symbol] = unit
        if unit == "share":
            trade = self.broker.place_odd_lot_order(
                symbol=symbol,
                action=Action.Buy,
                shares=quantity,
                price=price,
                custom_field=bot_buy_field(custom_field),
            )
        else:
            trade = self.broker.place_order(
                symbol=symbol,
                action=Action.Buy,
                quantity=quantity,
                price=price,
                custom_field=bot_buy_field(custom_field),
            )
        if trade is None:
            self.risk.release_entry_exposure(symbol)
            self._entry_units.pop(symbol, None)
            return False

        ordno = getattr(trade.order, "ordno", "")
        with self._pending_lock[symbol]:
            self.pending_orders[symbol].append(ordno)
        self._order_meta[ordno] = {
            "symbol": symbol,
            "action": "Buy",
            "custom_field": custom_field,
        }
        self._enter_placed.add(symbol)
        return True

    def _place_stop_sell(
        self,
        symbol: str,
        quantity: int,
        custom_field: str = "stop",
        *,
        llm_gate: str = "",
    ) -> bool:
        price = self._last_price.get(symbol, 0.0)
        pos = self.positions.get(symbol)
        if pos is None:
            return False
        quantity = min(quantity, pos.quantity)
        if quantity <= 0:
            return False

        if not self._can_auto_sell(symbol, quantity, custom_field, price=price):
            return False

        pnl_pct = self._position_pnl_pct(symbol, price) or 0.0
        exit_verdict = self.llm_gate.allow_exit(
            symbol, pos, price, pnl_pct, custom_field,
        )
        if not exit_verdict.allowed:
            self.logger.info(
                "🤖 AI 建議續抱，暫不賣出 %s [%s]: %s",
                symbol, custom_field, exit_verdict.summary,
            )
            self._record_sell_blocked(
                symbol, price, quantity, custom_field,
                pnl_pct=pnl_pct, llm_gate=exit_verdict.summary,
                unit=pos.unit,
            )
            return False

        llm_gate = exit_verdict.summary or llm_gate
        unit = pos.unit

        if not self._is_trade_mode:
            self._virtual_fill_sell(
                symbol, price, quantity, custom_field,
                unit=unit, llm_gate=llm_gate,
            )
            return True

        assert self.broker is not None
        if unit == "share":
            trade = self.broker.place_odd_lot_order(
                symbol=symbol,
                action=Action.Sell,
                shares=quantity,
                price=price,
                custom_field=bot_sell_field(custom_field),
                max_sell_qty=pos.quantity,
            )
        else:
            trade = self.broker.place_market_sell(
                symbol, quantity, bot_sell_field(custom_field),
                max_sell_qty=pos.quantity,
            )
        if trade is None:
            return False

        ordno = getattr(trade.order, "ordno", "")
        with self._pending_lock[symbol]:
            self.pending_orders[symbol].append(ordno)
        self._order_meta[ordno] = {
            "symbol": symbol,
            "action": "Sell",
            "custom_field": custom_field,
        }
        if custom_field == "close":
            self._closure_pending.add(symbol)
        return True

    def _sell_profit_target(self, symbol: str) -> float | None:
        targets = getattr(self.settings, "sell_profit_targets", {}) or {}
        target = targets.get(symbol)
        if target is None:
            return None
        return float(target)

    def _position_pnl_pct(self, symbol: str, price: float) -> float | None:
        pos = self.positions.get(symbol)
        if pos is None or pos.avg_price <= 0 or price <= 0:
            return None
        return position_net_pnl_pct(
            pos.avg_price, price, pos.quantity, pos.unit,
            settings=self.settings,
        )

    def _on_position_closed(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        quantity: int,
        unit: QtyUnit,
    ) -> None:
        """平倉後記錄賣出價、清除進場鎖，供回落買回。"""
        self._enter_placed.discard(symbol)
        self._peak_net_pnl.pop(symbol, None)
        net_pct = position_net_pnl_pct(
            entry_price, exit_price, quantity, unit,
            settings=self.settings,
        )
        self._symbol_exits[symbol] = SymbolExitRecord(
            exit_price=exit_price,
            exit_net_pnl_pct=net_pct,
            exit_time=now_tw(),
            quantity=quantity,
            unit=unit,
        )

    def _update_peak_net_pnl(self, symbol: str, price: float) -> tuple[float, float]:
        """回傳 (current_net_pnl, peak_net_pnl)。"""
        current = self._position_pnl_pct(symbol, price) or 0.0
        peak = self._peak_net_pnl.get(symbol, current)
        if current > peak:
            peak = current
            self._peak_net_pnl[symbol] = peak
        return current, peak

    def _in_profit_exit_window(self) -> bool:
        """是否處於午盤獲利平倉窗口 [profit_exit_start_time, exit_time)。"""
        start = getattr(self.settings, "profit_exit_start_time", None)
        if start is None:
            return False
        cur = now_tw_time()
        return start <= cur < self.settings.exit_time

    def _trailing_stop_triggered(self, symbol: str, price: float) -> bool:
        """淨利達門檻後，從淨利高點回撤超過 trailing_stop_pct。"""
        if self.llm_gate.has_limit_up_potential(symbol):
            return False
        current, peak = self._update_peak_net_pnl(symbol, price)
        if peak < self.settings.take_profit_pct:
            return False
        return (peak - current) >= self.settings.trailing_stop_pct

    def _can_rebuy(self, symbol: str, price: float, qty: int, unit: QtyUnit) -> bool:
        rec = self._symbol_exits.get(symbol)
        if rec is None:
            return False
        return rebuy_opportunity(
            rec.exit_price, price, qty, unit, settings=self.settings,
        )

    def _can_auto_sell(
        self,
        symbol: str,
        quantity: int,
        reason: str,
        *,
        price: float,
    ) -> bool:
        pos = self.positions.get(symbol)
        if pos is None or quantity <= 0:
            self.logger.warning("阻擋自動賣出：%s 無 AI 持倉或張數無效", symbol)
            return False
        if not is_bot_owner(pos.owner_tag):
            self.logger.warning(
                "阻擋自動賣出：%s owner_tag=%s 不是 AI 買進部位",
                symbol, pos.owner_tag,
            )
            return False

        target = self._sell_profit_target(symbol)
        if target is not None:
            pnl_pct = self._position_pnl_pct(symbol, price)
            if pnl_pct is None:
                self.logger.warning(
                    "阻擋自動賣出：%s 無法驗證 %.2f%% 賣出門檻",
                    symbol, target,
                )
                return False
            if pnl_pct < target:
                self.logger.info(
                    "阻擋自動賣出：%s PnL %.2f%% 尚未達使用者門檻 %.2f%% [%s]",
                    symbol, pnl_pct, target, reason,
                )
                return False

        return self.risk.check_exit(symbol, reason)

    def _record_sell_blocked(
        self,
        symbol: str,
        price: float,
        quantity: int,
        reason: str,
        *,
        pnl_pct: float,
        llm_gate: str,
        unit: QtyUnit,
    ) -> None:
        """watch/report 模式記錄 AI 阻擋賣出的訊號。"""
        if self._is_trade_mode:
            return
        prev_close = self._prev_close.get(symbol, 0.0)
        pct_chg = (
            100 * (price - prev_close) / prev_close if prev_close > 0 else 0.0
        )
        self.signal_recorder.record(SignalEvent(
            ts=now_tw(),
            symbol=symbol,
            action="sell-blocked",
            price=price,
            quantity=quantity,
            reason=reason,
            pct_chg=pct_chg,
            pnl_pct=pnl_pct,
            mode=self.settings.run_mode,
            source=self.settings.market_source,
            unit=unit,
            llm_gate=llm_gate,
        ))

    def _evaluate_standard_exits(self, symbol: str, price: float) -> bool:
        """使用者目標 / 移動停利 / 停損出場。若已下賣單回傳 True。"""
        if symbol not in self.positions or self._has_pending(symbol):
            return False
        pos = self.positions[symbol]
        pnl_pct = self._position_pnl_pct(symbol, price) or 0.0
        user_target_pct = self._sell_profit_target(symbol)

        if user_target_pct is not None and pnl_pct >= user_target_pct:
            self.logger.info(
                "[使用者目標賣出] %s 淨利=%.2f%% (>= %.2f%%)",
                symbol, pnl_pct, user_target_pct,
            )
            self._place_stop_sell(symbol, pos.quantity, custom_field="target")
            return True

        if self._in_profit_exit_window() and pnl_pct > 0:
            self.logger.info(
                "[午盤獲利平倉] %s 淨利=%.2f%% (窗口內有賺即出)",
                symbol, pnl_pct,
            )
            self._place_stop_sell(symbol, pos.quantity, custom_field="afternoon")
            return True

        if self._trailing_stop_triggered(symbol, price):
            current, peak = self._update_peak_net_pnl(symbol, price)
            self.logger.info(
                "[移動停利] %s 淨利=%.2f%% 高點=%.2f%%",
                symbol, current, peak,
            )
            self._place_stop_sell(symbol, pos.quantity, custom_field="trail")
            return True

        if pnl_pct <= self.settings.stop_loss_pct:
            self.logger.info(
                "[停損] %s 淨利=%.2f%% (<= %.1f%%)",
                symbol, pnl_pct, self.settings.stop_loss_pct,
            )
            self._place_stop_sell(symbol, pos.quantity, custom_field="sl")
            return True

        return False

    # ------------------------------------------------------------------
    # 抽象方法
    # ------------------------------------------------------------------

    @abstractmethod
    def on_tick(self, tick: MarketTick) -> None:
        """收到正規化 Tick 時的策略邏輯，由子類別實作。"""
        ...

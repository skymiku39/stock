"""可設定觸發規則 + LLM 閘門的當沖策略（僅做多、含費損益）。"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from bot.entry_rules import in_entry_range, resolve_entry_range
from bot.models import MarketTick
from bot.strategy import BaseStrategy
from bot.utils import now_tw_time

if TYPE_CHECKING:
    from bot.broker import SjBroker
    from bot.config import Settings
    from bot.market_source import TwsePublicMarketSource

_BUY_REASON_FIELDS = {
    "進場": "enter",
    "盤中再進": "reenter",
    "回落買回": "rebuy",
}


class ConfigurableStrategy(BaseStrategy):
    """env 驅動進出場 + LLM 閘門。

    進場 (至 exit_time 前，可同日多次):
      - 漲幅區間（預設 1%~5%，含邊界）；LLM 進場閘門可選
      - 平倉後漲幅再達標 → 盤中再進
      - 平倉後價格回落且淨利空間足夠 → 回落買回
    出場: 含費停損 / 淨利移動停利（漲停潛力可略過 2%）/ 使用者目標 / LLM 負向 / 收盤全出。
    """

    def __init__(
        self,
        broker: SjBroker | None,
        settings: Settings,
        market_source: TwsePublicMarketSource | None = None,
        logger: logging.Logger | None = None,
        publisher=None,
        *,
        wire_handlers: bool = True,
    ):
        super().__init__(
            broker, settings, market_source, logger,
            publisher=publisher, wire_handlers=wire_handlers,
        )
        self._last_watch_log: dict[str, float] = {}

    def _on_prev_close_ready(self, refs: dict[str, float]) -> None:
        self.logger.info("前日收盤已載入: %s", refs)

    def _buy_field_for_reason(self, reason: str) -> str:
        return _BUY_REASON_FIELDS.get(reason, "enter")

    def _try_buy(
        self,
        symbol: str,
        price: float,
        pct_chg: float,
        *,
        reason: str,
    ) -> None:
        verdict = self.llm_gate.allow_entry(symbol, price, pct_chg)
        if not verdict.allowed:
            self.logger.info(
                "[進場拒絕] %s 漲幅 %.2f%% — LLM 閘門: %s",
                symbol, pct_chg, verdict.reason,
            )
            return
        qty, unit = self._calc_quantity(price)
        if qty <= 0:
            return
        self.logger.info(
            "[%s] %s 漲幅 %.2f%% price=%.2f qty=%d %s llm=%s",
            reason, symbol, pct_chg, price, qty, unit, verdict.summary,
        )
        self._place_buy(
            symbol, price, qty,
            custom_field=self._buy_field_for_reason(reason),
            pct_chg=pct_chg,
            unit=unit,
            llm_gate=verdict.summary,
        )

    def _can_attempt_entry(self, symbol: str, cur_time) -> bool:
        """盤中至 exit_time 前、無持倉且無掛單時可嘗試進場。"""
        if cur_time >= self.settings.exit_time:
            return False
        if symbol in self._enter_placed or self._has_pending(symbol):
            return False
        if symbol in self.positions:
            return False
        if not getattr(self.settings, "allow_same_day_reentry", True):
            return symbol not in self._symbol_exits
        return True

    def _log_watch_status(
        self,
        symbol: str,
        pct_chg: float,
        cur_time,
    ) -> None:
        """空手時定期記錄漲幅與進場區間，方便盤中確認 BOT 有在盯。"""
        if symbol in self.positions or self._has_pending(symbol):
            return
        now = time.time()
        if now - self._last_watch_log.get(symbol, 0.0) < 120:
            return
        self._last_watch_log[symbol] = now
        lo, hi = resolve_entry_range(symbol, self.settings)
        hi_txt = f"{hi:.1f}" if hi > 0 else "不限"
        in_range = in_entry_range(symbol, pct_chg, self.settings)
        self.logger.info(
            "[盯盤] %s 漲幅 %+.2f%% 進場區間 %.1f~%s%% %s (至 %s)",
            symbol,
            pct_chg,
            lo,
            hi_txt,
            "符合" if in_range else "未達",
            self.settings.exit_time.strftime("%H:%M"),
        )

    def _try_intraday_entries(
        self,
        symbol: str,
        price: float,
        pct_chg: float,
        cur_time,
    ) -> None:
        if not self._can_attempt_entry(symbol, cur_time):
            return

        had_exit = symbol in self._symbol_exits

        # 回落買回優先（價格低於上次賣出且淨利空間足夠）
        if had_exit:
            qty, unit = self._calc_quantity(price)
            if qty > 0 and self._can_rebuy(symbol, price, qty, unit):
                self.logger.info(
                    "[回落買回] %s 現價 %.2f < 上次賣出 %.2f",
                    symbol, price, self._symbol_exits[symbol].exit_price,
                )
                self._try_buy(symbol, price, pct_chg, reason="回落買回")
                return

        # 漲幅達標：首次進場或平倉後盤中再進
        if in_entry_range(symbol, pct_chg, self.settings):
            reason = "盤中再進" if had_exit else "進場"
            self._try_buy(symbol, price, pct_chg, reason=reason)

    def on_tick(self, tick: MarketTick) -> None:
        symbol = tick.symbol
        price = tick.price
        self._last_price[symbol] = price
        cur_time = now_tw_time()

        if symbol not in self._prev_close:
            if tick.pct_chg:
                ref = price / (1 + tick.pct_chg / 100)
            else:
                ref = price
            self._prev_close[symbol] = ref

        prev_close = self._prev_close[symbol]
        if prev_close <= 0:
            return

        pct_chg = 100 * (price - prev_close) / prev_close

        self._log_watch_status(symbol, pct_chg, cur_time)
        self._try_intraday_entries(symbol, price, pct_chg, cur_time)

        # === 出場 ===
        if (
            cur_time < self.settings.exit_time
            and symbol in self.positions
            and not self._has_pending(symbol)
        ):
            pos = self.positions[symbol]
            pnl_pct = self._position_pnl_pct(symbol, price) or 0.0

            llm_exit = self.llm_gate.should_exit(symbol, pos)
            if llm_exit:
                self.logger.info(
                    "[LLM 負向出場] %s 淨利=%.2f%%", symbol, pnl_pct,
                )
                self._place_stop_sell(
                    symbol, pos.quantity,
                    custom_field=llm_exit,
                    llm_gate="negative_sentiment",
                )
            else:
                self._evaluate_standard_exits(symbol, price)

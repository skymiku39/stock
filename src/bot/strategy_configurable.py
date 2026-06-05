"""可設定觸發規則 + LLM 閘門的當沖策略。"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from bot.entry_rules import in_entry_range
from bot.llm_gate import LlmGate
from bot.models import MarketTick
from bot.strategy import BaseStrategy
from bot.utils import get_logger, now_tw_time

if False:  # TYPE_CHECKING
    from bot.broker import SjBroker
    from bot.config import Settings
    from bot.market_source import TwsePublicMarketSource


class ConfigurableStrategy(BaseStrategy):
    """env 驅動進出場 + LLM 閘門。

    進場: enter_cutoff_time 前，漲幅在 MIN/MAX 或 BUY_ENTRY_TARGETS 範圍內，
          且通過 LlmGate (若啟用)。
    出場: 停損 / 移動停利 / 使用者目標 / LLM 負向 / 收盤全出。
    """

    def __init__(
        self,
        broker: Optional["SjBroker"],
        settings: "Settings",
        market_source: Optional["TwsePublicMarketSource"] = None,
        logger: Optional[logging.Logger] = None,
    ):
        super().__init__(broker, settings, market_source, logger)
        self.llm_gate = LlmGate(
            settings=settings,
            risk=self.risk,
            logger=self.logger,
        )
        self._high_watermark: Dict[str, float] = {}

    def _on_prev_close_ready(self, refs: Dict[str, float]) -> None:
        self.logger.info("前日收盤已載入: %s", refs)

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

        # === 進場 ===
        if (
            cur_time < self.settings.enter_cutoff_time
            and symbol not in self._enter_placed
            and not self._has_pending(symbol)
            and symbol not in self.positions
            and in_entry_range(symbol, pct_chg, self.settings)
        ):
            verdict = self.llm_gate.allow_entry(symbol, price, pct_chg)
            if verdict.allowed:
                qty, unit = self._calc_quantity(price)
                if qty > 0:
                    self.logger.info(
                        "[進場] %s 漲幅 %.2f%% price=%.2f qty=%d %s llm=%s",
                        symbol, pct_chg, price, qty, unit, verdict.summary,
                    )
                    self._place_buy(
                        symbol, price, qty,
                        custom_field="enter",
                        pct_chg=pct_chg,
                        unit=unit,
                        llm_gate=verdict.summary,
                    )
            else:
                self.logger.debug(
                    "[LLM 擋下進場] %s 漲幅 %.2f%% — %s",
                    symbol, pct_chg, verdict.reason,
                )

        # === 出場 ===
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
            user_target_pct = self._sell_profit_target(symbol)

            llm_exit = self.llm_gate.should_exit(symbol, pos)
            if llm_exit:
                self.logger.info(
                    "[LLM 負向出場] %s PnL=%.2f%%", symbol, pnl_pct,
                )
                self._place_stop_sell(
                    symbol, pos.quantity,
                    custom_field=llm_exit,
                    llm_gate="negative_sentiment",
                )

            elif user_target_pct is not None and pnl_pct >= user_target_pct:
                self.logger.info(
                    "[使用者目標賣出] %s PnL=%.2f%% (>= %.2f%%)",
                    symbol, pnl_pct, user_target_pct,
                )
                self._place_stop_sell(symbol, pos.quantity, custom_field="target")

            elif (
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

"""SignalRecorder -- 記錄 watch/report 模式產生的交易意圖訊號。"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any, Dict, List, Optional

import pandas as pd

from bot.models import MarketTick, SignalEvent
from bot.utils import get_logger, mk_folder, now_tw


class SignalRecorder:
    """收集 SignalEvent，結束時匯出訊號 CSV 與分析報表。"""

    def __init__(
        self,
        output_dir: str = "data/reports",
        logger: Optional[logging.Logger] = None,
    ):
        self.output_dir = output_dir
        self.logger = logger or get_logger("signal-recorder")
        self._signals: List[SignalEvent] = []
        self._price_stats: Dict[str, Dict[str, float]] = {}

    # ------------------------------------------------------------------
    # 記錄
    # ------------------------------------------------------------------

    def record(self, event: SignalEvent) -> None:
        self._signals.append(event)
        self._update_price_stats(event.symbol, event.price)
        self.logger.debug(
            "Signal: %s %s %s @ %.2f x%d [%s]",
            event.action, event.symbol, event.reason,
            event.price, event.quantity, event.mode,
        )

    def record_tick(self, tick: MarketTick) -> None:
        """Track observed market highs/lows for report summaries."""
        self._update_price_stats(tick.symbol, tick.price)

    def _update_price_stats(self, symbol: str, price: float) -> None:
        if price <= 0:
            return
        stats = self._price_stats.setdefault(
            symbol, {"high": price, "low": price},
        )
        stats["high"] = max(stats["high"], price)
        stats["low"] = min(stats["low"], price)

    @property
    def signal_count(self) -> int:
        return len(self._signals)

    # ------------------------------------------------------------------
    # 匯出
    # ------------------------------------------------------------------

    def export_signals_csv(self) -> Optional[str]:
        """匯出 signals_YYYY-MM-DD.csv。"""
        if not self._signals:
            self.logger.info("無訊號紀錄，跳過匯出")
            return None

        mk_folder(self.output_dir)
        date_str = now_tw().strftime("%Y-%m-%d")
        filepath = os.path.join(self.output_dir, f"signals_{date_str}.csv")

        rows: List[Dict[str, Any]] = [dataclasses.asdict(s) for s in self._signals]
        df = pd.DataFrame(rows)
        df["ts"] = df["ts"].astype(str)
        df.to_csv(filepath, index=False, encoding="utf-8-sig")
        self.logger.info("訊號紀錄已匯出: %s (%d 筆)", filepath, len(df))
        return filepath

    def export_report(self) -> Optional[str]:
        """匯出 report_YYYY-MM-DD.csv，含每檔觸發次數、進出場價、分析損益。"""
        if not self._signals:
            return None

        mk_folder(self.output_dir)
        date_str = now_tw().strftime("%Y-%m-%d")
        filepath = os.path.join(self.output_dir, f"report_{date_str}.csv")

        rows: List[Dict[str, Any]] = [dataclasses.asdict(s) for s in self._signals]
        df = pd.DataFrame(rows)

        buys = df[df["action"] == "would-buy"]
        sells = df[df["action"] == "would-sell"]

        report_rows: List[Dict[str, Any]] = []
        symbols = df["symbol"].unique()
        for sym in symbols:
            sym_buys = buys[buys["symbol"] == sym]
            sym_sells = sells[sells["symbol"] == sym]
            entry_price = float(sym_buys["price"].iloc[0]) if len(sym_buys) else 0.0
            exit_price = float(sym_sells["price"].iloc[-1]) if len(sym_sells) else 0.0
            stats = self._price_stats.get(sym, {})

            pnl_pct = 0.0
            if entry_price > 0 and exit_price > 0:
                pnl_pct = 100 * (exit_price - entry_price) / entry_price

            report_rows.append({
                "symbol": sym,
                "buy_signals": len(sym_buys),
                "sell_signals": len(sym_sells),
                "first_entry_price": entry_price,
                "last_exit_price": exit_price,
                "high": float(stats.get("high", 0.0)),
                "low": float(stats.get("low", 0.0)),
                "pnl_pct": round(pnl_pct, 2),
            })

        rdf = pd.DataFrame(report_rows)
        rdf.to_csv(filepath, index=False, encoding="utf-8-sig")
        self.logger.info("分析報表已匯出: %s", filepath)
        return filepath

    # ------------------------------------------------------------------
    # 文字摘要
    # ------------------------------------------------------------------

    def summary(self) -> str:
        if not self._signals:
            return "今日無訊號"

        buys = [s for s in self._signals if s.action == "would-buy"]
        sells = [s for s in self._signals if s.action == "would-sell"]
        symbols = sorted({s.symbol for s in self._signals})

        total_buy_amt = sum(s.price * s.quantity * 1000 for s in buys)
        total_sell_amt = sum(s.price * s.quantity * 1000 for s in sells)

        lines = [
            f"模式: {self._signals[0].mode}",
            f"訊號總數: {len(self._signals)} (買 {len(buys)} / 賣 {len(sells)})",
            f"虛擬買入金額: {total_buy_amt:,.0f}",
            f"虛擬賣出金額: {total_sell_amt:,.0f}",
            f"商品: {', '.join(symbols)}",
        ]

        for sym in symbols:
            sym_sells = [s for s in sells if s.symbol == sym]
            if sym_sells:
                last_pnl = sym_sells[-1].pnl_pct
                lines.append(f"  {sym} 最終分析損益: {last_pnl:+.2f}%")

        return "\n".join(lines)

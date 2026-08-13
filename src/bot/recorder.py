"""TradeRecorder -- 收集成交回報，收盤後匯出 CSV。"""

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

from bot.models import QtyUnit, qty_multiplier
from bot.ownership import infer_owner_tag
from bot.utils import get_logger, mk_folder, now_tw


class TradeRecorder:
    """將每筆成交紀錄暫存於記憶體，結束時匯出為 CSV。"""

    def __init__(self, output_dir: str = "data", logger: logging.Logger | None = None):
        self.output_dir = output_dir
        self.logger = logger or get_logger("recorder")
        self._records: list[dict[str, Any]] = []

    def record_deal(
        self,
        msg: dict,
        *,
        unit: QtyUnit = "lot",
        trade_reason: str = "",
        entry_price: float | None = None,
        pnl_pct: float | None = None,
        pnl_twd: float | None = None,
    ) -> None:
        """從 Shioaji StockDeal callback msg 擷取欄位並暫存。"""
        custom_field = msg.get("custom_field", "")
        record: dict[str, Any] = {
            "datetime": now_tw().isoformat(timespec="seconds"),
            "symbol": msg.get("code", ""),
            "action": msg.get("action", ""),
            "price": float(msg.get("price", 0)),
            "quantity": int(msg.get("quantity", 0)),
            "unit": unit,
            "ordno": msg.get("ordno", ""),
            "custom_field": custom_field,
            "owner_tag": infer_owner_tag(custom_field=custom_field),
        }
        if trade_reason:
            record["trade_reason"] = trade_reason
        if entry_price is not None:
            record["entry_price"] = round(float(entry_price), 4)
        if pnl_pct is not None:
            record["pnl_pct"] = round(float(pnl_pct), 4)
        if pnl_twd is not None:
            record["pnl_twd"] = round(float(pnl_twd), 2)
        self._records.append(record)

    @property
    def deal_count(self) -> int:
        return len(self._records)

    def export_csv(self) -> str | None:
        """匯出所有紀錄為 CSV，回傳檔案路徑。無紀錄時回傳 None。"""
        if not self._records:
            self.logger.info("無成交紀錄，跳過匯出")
            return None

        mk_folder(self.output_dir)
        date_str = now_tw().strftime("%Y-%m-%d")
        filepath = os.path.join(self.output_dir, f"trades_{date_str}.csv")

        df = pd.DataFrame(self._records)

        if "unit" not in df.columns:
            df["unit"] = "lot"
        df["amount"] = df.apply(
            lambda r: r["price"] * r["quantity"] * qty_multiplier(r.get("unit", "lot")),
            axis=1,
        )

        df.to_csv(filepath, index=False, encoding="utf-8-sig")
        self.logger.info("交易紀錄已匯出: %s (%d 筆)", filepath, len(df))
        return filepath

    def summary(self) -> str:
        """產生簡要統計摘要。"""
        if not self._records:
            return "今日無成交"

        df = pd.DataFrame(self._records)
        if "unit" not in df.columns:
            df["unit"] = "lot"
        buys = df[df["action"] == "Buy"]
        sells = df[df["action"] == "Sell"]
        total_buy = (
            buys.apply(
                lambda r: r["price"] * r["quantity"] * qty_multiplier(r.get("unit", "lot")),
                axis=1,
            ).sum()
            if len(buys) else 0
        )
        total_sell = (
            sells.apply(
                lambda r: r["price"] * r["quantity"] * qty_multiplier(r.get("unit", "lot")),
                axis=1,
            ).sum()
            if len(sells) else 0
        )

        lines = [
            f"成交筆數: {len(df)}",
            f"買進金額: {total_buy:,.0f}",
            f"賣出金額: {total_sell:,.0f}",
            f"商品: {', '.join(df['symbol'].unique())}",
        ]
        return "\n".join(lines)

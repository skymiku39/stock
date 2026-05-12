"""TradeRecorder -- 收集成交回報，收盤後匯出 CSV。"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import pandas as pd

from bot.utils import get_logger, mk_folder, now_tw


class TradeRecorder:
    """將每筆成交紀錄暫存於記憶體，結束時匯出為 CSV。"""

    def __init__(self, output_dir: str = "data", logger: Optional[logging.Logger] = None):
        self.output_dir = output_dir
        self.logger = logger or get_logger("recorder")
        self._records: List[Dict[str, Any]] = []

    def record_deal(self, msg: dict) -> None:
        """從 Shioaji StockDeal callback msg 擷取欄位並暫存。"""
        self._records.append({
            "datetime": now_tw().isoformat(timespec="seconds"),
            "symbol": msg.get("code", ""),
            "action": msg.get("action", ""),
            "price": float(msg.get("price", 0)),
            "quantity": int(msg.get("quantity", 0)),
            "ordno": msg.get("ordno", ""),
            "custom_field": msg.get("custom_field", ""),
        })

    @property
    def deal_count(self) -> int:
        return len(self._records)

    def export_csv(self) -> Optional[str]:
        """匯出所有紀錄為 CSV，回傳檔案路徑。無紀錄時回傳 None。"""
        if not self._records:
            self.logger.info("無成交紀錄，跳過匯出")
            return None

        mk_folder(self.output_dir)
        date_str = now_tw().strftime("%Y-%m-%d")
        filepath = os.path.join(self.output_dir, f"trades_{date_str}.csv")

        df = pd.DataFrame(self._records)

        # 計算每筆成交金額
        df["amount"] = df["price"] * df["quantity"] * 1000

        df.to_csv(filepath, index=False, encoding="utf-8-sig")
        self.logger.info("交易紀錄已匯出: %s (%d 筆)", filepath, len(df))
        return filepath

    def summary(self) -> str:
        """產生簡要統計摘要。"""
        if not self._records:
            return "今日無成交"

        df = pd.DataFrame(self._records)
        buys = df[df["action"] == "Buy"]
        sells = df[df["action"] == "Sell"]
        total_buy = (buys["price"] * buys["quantity"] * 1000).sum() if len(buys) else 0
        total_sell = (sells["price"] * sells["quantity"] * 1000).sum() if len(sells) else 0

        lines = [
            f"成交筆數: {len(df)}",
            f"買進金額: {total_buy:,.0f}",
            f"賣出金額: {total_sell:,.0f}",
            f"商品: {', '.join(df['symbol'].unique())}",
        ]
        return "\n".join(lines)

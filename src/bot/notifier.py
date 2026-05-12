"""TelegramNotifier -- 透過 Telegram Bot API 推播通知。"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import requests

from bot.utils import get_logger


class TelegramNotifier:
    """非同步 (背景執行緒) 發送 Telegram 訊息，避免阻塞主流程。"""

    SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        logger: Optional[logging.Logger] = None,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.logger = logger or get_logger("notifier")
        self._enabled = bool(bot_token and chat_id)

        if not self._enabled:
            self.logger.info("Telegram 通知未設定 (BOT_TOKEN 或 CHAT_ID 為空)，跳過")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(self, text: str) -> None:
        """在背景執行緒發送訊息。"""
        if not self._enabled:
            return
        threading.Thread(
            target=self._do_send, args=(text,), daemon=True,
        ).start()

    def _do_send(self, text: str) -> None:
        url = self.SEND_URL.format(token=self.bot_token)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if not resp.ok:
                self.logger.warning("Telegram 發送失敗: %s", resp.text)
        except Exception:
            self.logger.exception("Telegram 發送例外")

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def notify_start(self, symbols: list[str], simulation: bool) -> None:
        mode = "模擬" if simulation else "實單"
        self.send(f"🤖 <b>Stock Bot 啟動</b> [{mode}]\n監控: {', '.join(symbols)}")

    def notify_buy(self, symbol: str, price: float, qty: int) -> None:
        self.send(f"📈 <b>買進</b> {symbol}\n價格: {price:.2f} | 張數: {qty}")

    def notify_sell(self, symbol: str, price: float, qty: int, reason: str) -> None:
        label = {"sl": "停損", "tp": "停利", "trail": "移動停利", "close": "收盤出場"}.get(reason, reason)
        self.send(f"📉 <b>賣出 ({label})</b> {symbol}\n價格: {price:.2f} | 張數: {qty}")

    def notify_closure(self, summary: str) -> None:
        self.send(f"📋 <b>收盤結算</b>\n<pre>{summary}</pre>")

    def notify_error(self, error: str) -> None:
        self.send(f"⚠️ <b>異常</b>\n{error}")

    def notify_shutdown(self) -> None:
        self.send("🛑 <b>Stock Bot 已停止</b>")

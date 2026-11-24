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

    def send_plain(self, text: str) -> bool:
        """同步發送純文字（不做 HTML 解析），回傳是否成功；未設定回 False。不拋例外。"""
        if not self._enabled:
            return False
        return self._post({"chat_id": self.chat_id, "text": text})

    def _do_send(self, text: str) -> None:
        self._post({"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"})

    def _post(self, payload: dict) -> bool:
        url = self.SEND_URL.format(token=self.bot_token)
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if not resp.ok:
                self.logger.warning("Telegram 發送失敗: %s", resp.text)
                return False
            return True
        except Exception:
            # 例外訊息可能含 token 的 URL，不記內容
            self.logger.warning("Telegram 發送例外")
            return False

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def notify_start(
        self,
        symbols: list[str],
        simulation: bool,
        run_mode: str = "trade",
    ) -> None:
        if run_mode == "watch":
            mode = "看盤"
        elif run_mode == "report":
            mode = "報表"
        else:
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


def main(argv=None) -> int:
    """python -m bot.notifier --test "訊息"：送一則測試訊息，只印結果，不印 token。"""
    import argparse

    from bot.config import Settings
    from bot.utils import configure_utf8_stdio

    configure_utf8_stdio()
    p = argparse.ArgumentParser(description="Telegram 通知測試")
    p.add_argument("--test", metavar="訊息", required=True, help="送出的測試訊息")
    args = p.parse_args(argv)
    s = Settings()
    n = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)
    if not n.enabled:
        print("Telegram 未設定（TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 為空），未送出")
        return 1
    if n.send_plain(args.test):
        print("Telegram 已送出")
        return 0
    print("Telegram 送出失敗（檢查 token、chat id，或先對 bot 按 Start）")
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())

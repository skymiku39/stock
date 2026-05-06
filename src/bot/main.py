"""程式進入點 -- 載入設定、登入、啟動策略。"""

from __future__ import annotations

import signal
import sys

from bot.broker import SjBroker
from bot.config import Settings
from bot.strategy import MyStrategy
from bot.utils import get_logger


def main() -> None:
    logger = get_logger("main")
    logger.info("=== Stock Bot 啟動 ===")

    settings = Settings()
    logger.info("組態載入完成 (simulation=%s)", settings.simulation)

    if not settings.symbols:
        logger.error("未設定監控股票 (SYMBOLS)，請檢查 .env")
        sys.exit(1)

    broker = SjBroker(settings)

    if not broker.login():
        logger.error("登入失敗，程式結束")
        sys.exit(1)

    strategy = MyStrategy(broker, settings)

    def _shutdown(signum: int, frame: object) -> None:
        logger.info("收到信號 %d，關閉中 ...", signum)
        strategy.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        strategy.run()
    finally:
        broker.logout()
        logger.info("=== Stock Bot 結束 ===")


if __name__ == "__main__":
    main()

"""程式進入點 -- 載入設定、依模式啟動策略。

模式:
  trade  - 完整自動交易 (Shioaji 登入 + CA + 下單)
  watch  - 看盤模式 (Shioaji 行情，不啟用 CA、不送單，記錄 would-buy/sell)
  report - 報表模式 (不登入 Shioaji，輪詢公開延遲資料，產出分析報表)
"""

from __future__ import annotations

import signal
import sys

from bot.config import Settings
from bot.utils import get_logger


def main() -> None:
    logger = get_logger("main")
    logger.info("=== Stock Bot 啟動 ===")

    settings = Settings()
    logger.info(
        "組態載入完成 (run_mode=%s, market_source=%s, simulation=%s)",
        settings.run_mode,
        settings.market_source,
        settings.simulation,
    )

    if not settings.symbols:
        logger.error("未設定監控股票 (SYMBOLS)，請檢查 .env")
        sys.exit(1)

    broker = None
    market_source = None

    # ---- trade / watch: 需要 Shioaji ----
    if settings.run_mode in ("trade", "watch"):
        from bot.broker import SjBroker

        if not settings.api_key or not settings.secret_key:
            logger.error(
                "run_mode=%s 需要 API_KEY 與 SECRET_KEY，請檢查 .env",
                settings.run_mode,
            )
            sys.exit(1)

        broker = SjBroker(settings)
        if not broker.login():
            logger.error("登入失敗，程式結束")
            sys.exit(1)

    # ---- report: 公開資料來源 ----
    if settings.run_mode == "report":
        from bot.market_source import TwsePublicMarketSource

        market_source = TwsePublicMarketSource(
            symbols=settings.symbols,
            poll_seconds=settings.report_poll_seconds,
        )
        logger.info(
            "report 模式 — 使用 TWSE 公開延遲資料 (輪詢 %ds)",
            settings.report_poll_seconds,
        )

    # ---- 建立策略 ----
    if settings.strategy_type == "etf_follow":
        from bot.strategy_etf_follow import EtfFollowStrategy

        strategy = EtfFollowStrategy(
            broker=broker,
            settings=settings,
            market_source=market_source,
            min_consensus_new=settings.etf_min_consensus_new,
            min_consensus_add=settings.etf_min_consensus_add,
            max_pct_chg_on_entry=settings.etf_max_pct_chg_on_entry,
        )
        logger.info(
            "策略: EtfFollowStrategy (new>=%d, add>=%d, max_pct=%.1f)",
            settings.etf_min_consensus_new,
            settings.etf_min_consensus_add,
            settings.etf_max_pct_chg_on_entry,
        )
    else:
        from bot.strategy import MyStrategy

        strategy = MyStrategy(
            broker=broker,
            settings=settings,
            market_source=market_source,
        )
        logger.info("策略: MyStrategy (預設當沖示範)")

    def _shutdown(signum: int, frame: object) -> None:
        logger.info("收到信號 %d，關閉中 ...", signum)
        strategy.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        strategy.run()
    finally:
        # ---- trade 模式: 匯出交易紀錄 ----
        if settings.run_mode == "trade":
            strategy.recorder.export_csv()
            summary = strategy.recorder.summary()
            logger.info("交易摘要:\n%s", summary)

        # ---- watch / report 模式: 匯出訊號與報表 ----
        if settings.run_mode in ("watch", "report"):
            strategy.signal_recorder.export_signals_csv()
            strategy.signal_recorder.export_report()
            summary = strategy.signal_recorder.summary()
            logger.info("訊號摘要:\n%s", summary)

        strategy.notifier.notify_shutdown()

        if broker is not None:
            broker.logout()

        logger.info("=== Stock Bot 結束 ===")


if __name__ == "__main__":
    main()

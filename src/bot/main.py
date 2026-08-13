"""程式進入點 -- 載入設定、依模式啟動策略。

模式:
  trade  - 完整自動交易 (Shioaji 登入 + CA + 下單)
  watch  - 看盤模式 (Shioaji 行情，不啟用 CA、不送單，記錄 would-buy/sell)
  report - 報表模式 (不登入 Shioaji，輪詢公開延遲資料，產出分析報表)
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from bot.app_bootstrap import bootstrap_event_bus
from bot.archive_status import day_trading_trade_blocked, trade_block_message
from bot.config import Settings
from bot.events import BotShutdownRequested
from bot.events.wiring import publish_if_bus
from bot.utils import get_logger

_INSTANCE_LOCK = Path("data/bot.instance.pid")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_instance_lock(logger) -> None:
    """trade 模式避免多個 BOT 同時下單。"""
    _INSTANCE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()
    if _INSTANCE_LOCK.exists():
        try:
            other = int(_INSTANCE_LOCK.read_text(encoding="utf-8").strip())
        except ValueError:
            other = 0
        if other and other != my_pid and _pid_alive(other):
            logger.error("已有 BOT 執行中 (PID %s)，請先停止再啟動", other)
            sys.exit(1)
    _INSTANCE_LOCK.write_text(str(my_pid), encoding="utf-8")


def _release_instance_lock() -> None:
    if not _INSTANCE_LOCK.exists():
        return
    try:
        if int(_INSTANCE_LOCK.read_text(encoding="utf-8").strip()) == os.getpid():
            _INSTANCE_LOCK.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def main() -> None:
    logger = get_logger("main")
    logger.info("=== Stock Bot 啟動 ===")

    event_bus = bootstrap_event_bus()

    settings = Settings()
    if day_trading_trade_blocked(settings):
        logger.error(trade_block_message(settings))
        sys.exit(1)
    if settings.run_mode == "trade":
        _acquire_instance_lock(logger)
    logger.info(
        "組態載入完成 (run_mode=%s, market_source=%s, simulation=%s)",
        settings.run_mode,
        settings.market_source,
        settings.simulation,
    )

    if getattr(settings, "symbols_auto_merge", True):
        from bot.watch_symbol_pool import merge_watch_symbols_into_settings

        merge_watch_symbols_into_settings(
            settings,
            Path.cwd(),
            reason="main",
            refresh_live_quotes=False,
            logger=logger,
        )

    if not settings.symbols:
        logger.error(
            "監控池為空：請設定 SYMBOLS 或啟用 SYMBOLS_AUTO_MERGE 並確保戰情室/明日關注報告存在",
        )
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
            publisher=event_bus,
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
        from bot.strategy_configurable import ConfigurableStrategy

        strategy = ConfigurableStrategy(
            broker=broker,
            settings=settings,
            market_source=market_source,
            publisher=event_bus,
        )
        logger.info(
            "策略: ConfigurableStrategy (min_pct=%.1f, odd_lot=%s, "
            "llm_entry=%s, llm_sell=%s, daily_budget=%d)",
            settings.min_pct_chg_on_entry,
            settings.use_odd_lot,
            settings.llm_gate_enabled,
            settings.llm_sell_gate_enabled,
            settings.effective_fund_cap(),
        )

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

        trade_summary = ""
        if settings.run_mode == "trade":
            trade_summary = strategy.recorder.summary()
        publish_if_bus(
            event_bus,
            BotShutdownRequested(
                run_mode=settings.run_mode,
                trade_summary=trade_summary,
            ),
        )

        if broker is not None:
            broker.logout()

        _release_instance_lock()
        logger.info("=== Stock Bot 結束 ===")


if __name__ == "__main__":
    main()

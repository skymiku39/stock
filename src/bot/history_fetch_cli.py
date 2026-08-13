"""stock-history-fetch -- 慢速自動補齊歷史日 K（排程友善）。

用法
====
    uv run stock-history-fetch --status
    uv run stock-history-fetch --init --start 2020-01-01
    uv run stock-history-fetch --once --batch-size 1 --delay 3
    uv run stock-history-fetch --daemon --batch-size 1 --sleep 120

整合 stock-scheduler（.env）::

    SCHEDULER_HISTORY_FETCH_INTERVAL_MIN=5
    SCHEDULER_HISTORY_FETCH_ARGS=--once --batch-size 1 --delay 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from bot.history_fetch_queue import (
    init_state,
    process_batch,
    status_summary,
)
from bot.utils import get_logger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="慢速補齊歷史日 K")
    parser.add_argument("--init", action="store_true", help="重建待補佇列")
    parser.add_argument("--status", action="store_true", help="顯示進度")
    parser.add_argument("--once", action="store_true", help="跑一批後結束")
    parser.add_argument("--daemon", action="store_true", help="常駐循環（Ctrl+C 結束）")
    parser.add_argument("--start", default="2020-01-01", help="歷史起點")
    parser.add_argument("--end", default="", help="歷史終點 (預設今天)")
    parser.add_argument("--symbols", default="", help="覆寫股票清單 (逗號分隔)")
    parser.add_argument("--lookback", type=int, default=5, help="LLM 報告往回天數")
    parser.add_argument("--batch-size", type=int, default=3, help="每批抓取月數")
    parser.add_argument("--delay", type=float, default=5.0, help="每次請求間隔秒")
    parser.add_argument("--sleep", type=float, default=30.0, help="daemon 每批間隔秒")
    parser.add_argument("--cooldown", type=int, default=45, help="403 後冷卻分鐘")
    parser.add_argument("--reinit-cycle", action="store_true", help="跑完一輪後重新掃描")
    parser.add_argument(
        "--no-yfinance-fallback", action="store_true",
        help="TWSE 403 時不改用 yfinance",
    )
    args = parser.parse_args(argv)

    logger = get_logger("history-fetch")
    root = Path.cwd()

    if args.status:
        summary = status_summary(root)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.init:
        syms = (
            [s.strip() for s in args.symbols.split(",") if s.strip()]
            if args.symbols else None
        )
        state = init_state(
            root=root,
            start_date=args.start,
            end_date=args.end or None,
            symbols=syms,
            lookback_days=args.lookback,
        )
        logger.info(
            "佇列已初始化：%d 檔，%d 個月份區間 (%s ~ %s)",
            len(state.symbols),
            len(state.months),
            state.start_date,
            state.end_date,
        )
        if not args.once and not args.daemon:
            return 0

    if not args.once and not args.daemon:
        parser.print_help()
        return 2

    def _run_batch() -> int:
        results = process_batch(
            root=root,
            batch_size=args.batch_size,
            delay_sec=args.delay,
            cooldown_minutes=args.cooldown,
            reinit_if_done=args.reinit_cycle,
            lookback_days=args.lookback,
            use_yfinance_fallback=not args.no_yfinance_fallback,
            logger=logger,
        )
        for r in results:
            if r.item:
                tag = "OK" if r.ok else "FAIL"
                logger.info(
                    "[%s] %s %04d/%02d — %s",
                    tag,
                    r.item.symbol,
                    r.item.year,
                    r.item.month,
                    r.message,
                )
        return 0 if results else 0

    if args.once:
        return _run_batch()

    logger.info(
        "Daemon 啟動 batch=%d delay=%.1fs sleep=%.0fs",
        args.batch_size, args.delay, args.sleep,
    )
    try:
        while True:
            _run_batch()
            summary = status_summary(root)
            logger.info(
                "進度 ok=%s fail=%s rows=%s pending≈%s last=%s",
                summary["stats_ok"],
                summary["stats_fail"],
                summary["stats_rows"],
                summary["pending_months_est"],
                summary["last_item"],
            )
            time.sleep(max(10.0, args.sleep))
    except KeyboardInterrupt:
        logger.info("已停止")
        return 0


if __name__ == "__main__":
    sys.exit(main())

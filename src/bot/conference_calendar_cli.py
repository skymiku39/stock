"""conference_calendar_cli -- 純行事曆自動抓取 CLI。

用途：放進排程 (Windows Task Scheduler / cron)，每天早上跑一次自動更新
法說會行事曆與全球科技事件，dashboard 與 ``stock-auto-research --llm-only`` 都會直接吃這份快取。

用法
====
```
uv run stock-calendar-update                # MOPS + 全球科技事件
uv run stock-calendar-update --global-only  # 只更新全球科技事件
uv run stock-calendar-update --months -2 0 1 2 3
uv run stock-calendar-update --upcoming 14  # 抓完印出未來 14 天的法說會與全球事件
```
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bot.conference_calendar import (
    upcoming_conferences,
    update_calendar,
)
from bot.global_event_calendar import (
    upcoming_global_events,
    update_global_events,
)
from bot.utils import get_logger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-calendar-update",
        description="自動抓 MOPS 法說會行事曆與全球科技事件 (寫到 data/calendar/)",
    )
    parser.add_argument(
        "--months", nargs="+", type=int, default=[-1, 0, 1, 2],
        help="相對於今日的月份偏移 (預設 -1 0 1 2)",
    )
    parser.add_argument(
        "--upcoming", type=int, default=0,
        help="抓完後列出未來 N 天的法說會與全球事件 (0 = 不列)",
    )
    parser.add_argument(
        "--global-only", action="store_true",
        help="只更新全球科技事件，略過 MOPS 法說會",
    )
    parser.add_argument(
        "--no-global", action="store_true",
        help="只更新 MOPS 法說會，略過全球科技事件",
    )
    args = parser.parse_args(argv)

    logger = get_logger("calendar")
    root = Path.cwd()
    months = tuple(args.months)

    if not args.global_only:
        logger.info("=== 法說會行事曆自動更新 (月份偏移: %s) ===", months)
        summary = update_calendar(root=root, months=months, logger=logger)
        logger.info("法說會完成: %s", summary)

    if not args.no_global:
        logger.info("=== 全球科技事件自動更新 ===")
        global_summary = update_global_events(root=root, logger=logger)
        logger.info("全球科技事件完成: %d 筆", global_summary.get("count", 0))

    if args.upcoming > 0:
        conf_items = upcoming_conferences(days=args.upcoming, root=root)
        logger.info("未來 %d 天法說會共 %d 場：", args.upcoming, len(conf_items))
        for e in conf_items:
            logger.info(
                "  %s %s  %s %s  %s",
                e.date.isoformat(), e.time or "----",
                e.ticker, e.company, e.note or "",
            )
        global_items = upcoming_global_events(days=args.upcoming, root=root)
        logger.info("未來 %d 天全球科技事件共 %d 場：", args.upcoming, len(global_items))
        for e in global_items:
            tickers = ", ".join(e.tickers) if e.tickers else "-"
            logger.info(
                "  %s ~ %s  %s  (台股: %s)",
                e.date.isoformat(), e.effective_end_date.isoformat(),
                e.title, tickers,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())

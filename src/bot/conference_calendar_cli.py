"""conference_calendar_cli -- 純行事曆自動抓取 CLI。

用途：放進排程 (Windows Task Scheduler / cron)，每天早上跑一次自動更新
法說會行事曆，dashboard 與 ``stock-auto-research --llm-only`` 都會直接吃這份快取。

用法
====
```
uv run stock-calendar-update                # 自動抓 (上月 / 本月 / 下月 / +2 月)
uv run stock-calendar-update --months -2 0 1 2 3
uv run stock-calendar-update --upcoming 14  # 抓完印出未來 14 天的法說會
```
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from bot.conference_calendar import (
    update_calendar,
    upcoming_conferences,
)
from bot.utils import get_logger


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-calendar-update",
        description="自動抓 MOPS 法說會行事曆 (寫到 data/calendar/)",
    )
    parser.add_argument(
        "--months", nargs="+", type=int, default=[-1, 0, 1, 2],
        help="相對於今日的月份偏移 (預設 -1 0 1 2)",
    )
    parser.add_argument(
        "--upcoming", type=int, default=0,
        help="抓完後列出未來 N 天的法說會 (0 = 不列)",
    )
    args = parser.parse_args(argv)

    logger = get_logger("calendar")
    root = Path.cwd()
    months = tuple(args.months)
    logger.info("=== 法說會行事曆自動更新 (月份偏移: %s) ===", months)
    summary = update_calendar(root=root, months=months, logger=logger)
    logger.info("完成: %s", summary)

    if args.upcoming > 0:
        items = upcoming_conferences(days=args.upcoming, root=root)
        logger.info("未來 %d 天共 %d 場：", args.upcoming, len(items))
        for e in items:
            logger.info(
                "  %s %s  %s %s  %s",
                e.date.isoformat(), e.time or "----",
                e.ticker, e.company, e.note or "",
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())

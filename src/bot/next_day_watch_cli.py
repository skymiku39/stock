"""stock-nextday CLI -- 一鍵跑「明日當沖預備清單」管線。

用法
====
    uv run stock-nextday                       # 預設 draft (盤後初版)
    uv run stock-nextday --mode update         # 凌晨更新版 (重新抓 macro)
    uv run stock-nextday --refresh-news        # 強制重抓新聞
    uv run stock-nextday --limit 30            # 排序輸出 30 檔
    uv run stock-nextday --scan-limit 80       # 強勢承接掃描範圍最多 80 檔
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from bot.config import Settings
from bot.next_day_watch_pipeline import run_next_day_watch
from bot.utils import get_logger


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-nextday",
        description="明日當沖預備清單 (題材延續 + 強勢承接 + 明日事件)",
    )
    parser.add_argument(
        "--mode", choices=["draft", "update"], default="draft",
        help="draft=盤後初版 (預設) / update=凌晨更新版",
    )
    parser.add_argument("--refresh-news", action="store_true", help="強制重抓新聞")
    parser.add_argument("--refresh-macro", action="store_true", help="強制重抓 macro (建議 update 模式打開)")
    parser.add_argument("--news-limit", type=int, default=120, help="新聞抓取數量")
    parser.add_argument("--limit", type=int, default=25, help="候選股排序輸出數量")
    parser.add_argument("--scan-limit", type=int, default=60, help="強勢承接掃描範圍上限")
    args = parser.parse_args(argv)

    logger = get_logger("stock-nextday")
    settings = Settings()

    refresh_macro = args.refresh_macro or args.mode == "update"

    report = run_next_day_watch(
        mode=args.mode,
        project_root=Path.cwd(),
        settings=settings,
        news_limit=args.news_limit,
        candidate_limit=args.limit,
        scan_limit=args.scan_limit,
        force_refresh_news=args.refresh_news,
        force_refresh_macro=refresh_macro,
        logger=logger,
    )

    logger.info(
        "=== 完成 (%s): 題材 %d / 事件 %d / 候選 %d / 錯誤 %d / 簡報 %d bytes / %.1fs ===",
        report.mode,
        len(report.carry_themes), len(report.event_focus),
        len(report.rankings), len(report.errors),
        len(report.brief_md), report.duration_sec,
    )
    logger.info("目標日: %s ｜ 輸出目錄: %s", report.target_date, report.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

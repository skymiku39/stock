"""stock-intraday CLI — 一鍵跑當沖戰情室管線。

用法
====
    uv run stock-intraday                       # 預設：用快取的 macro/news
    uv run stock-intraday --refresh-news        # 強制重抓新聞
    uv run stock-intraday --limit 30            # 候選股最多 30 檔
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from bot.cli_encoding import configure_console
from bot.app_bootstrap import get_or_create_bus
from bot.config import Settings
from bot.intraday_pipeline import run_intraday
from bot.utils import log_root
from bot.utils import get_logger


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(
        prog="stock-intraday",
        description="今日當沖戰情室 (主題雷達 → 候選股 → 排序 → LLM 簡報)",
    )
    parser.add_argument("--refresh-news", action="store_true", help="強制重抓新聞")
    parser.add_argument("--news-limit", type=int, default=120, help="新聞抓取數量")
    parser.add_argument("--limit", type=int, default=25, help="候選股輸出數量")
    parser.add_argument(
        "--root", type=Path, default=None,
        help="專案資料／設定根目錄（未指定時使用目前工作目錄）",
    )
    args = parser.parse_args(argv)

    project_root = args.root.resolve() if args.root else Path.cwd()
    logger = get_logger("stock-intraday", log_dir=project_root / "log")
    settings = Settings(_env_file=str(project_root / ".env"))  # type: ignore[call-arg]
    with log_root(project_root):
        report = run_intraday(
            project_root=project_root,
            settings=settings,
            news_limit=args.news_limit,
            candidate_limit=args.limit,
            force_refresh_news=args.refresh_news,
            logger=logger,
            publisher=get_or_create_bus(project_root=project_root),
        )

    logger.info(
        "=== 完成: 題材 %d / 候選 %d / 錯誤 %d / 簡報 %d bytes / 用時 %.1fs ===",
        len(report.themes), len(report.rankings), len(report.errors),
        len(report.brief_md), report.duration_sec,
    )
    logger.info("輸出目錄: %s", report.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""stock-company-update -- 一次補齊全市場公司基本資料 (名稱/簡稱/產業/上市日)。

解決「目前持股分析 / 查資料頁面大量顯示未分類、名稱空白」的問題：
從 TWSE / TPEx 公開 OpenAPI 抓上市+上櫃公司基本資料，把「產業別代碼」轉成中文後
寫進本地 SQLite 的 stock_info。

用法:
    uv run stock-company-update           # 只補缺漏 (不覆蓋既有人工資料)
    uv run stock-company-update --all      # 全部覆寫 (以官方資料為準)
    uv run stock-company-update --refresh  # 強制重抓來源 (略過當日快取)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bot.company_info import backfill_stock_info, load_company_map
from bot.config import Settings
from bot.stock_db import StockDB, default_db_path
from bot.utils import get_logger


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(prog="stock-company-update")
    parser.add_argument("--all", action="store_true", help="全部覆寫 (不只補缺漏)")
    parser.add_argument("--refresh", action="store_true", help="強制重抓來源，略過當日快取")
    args = parser.parse_args(argv)

    logger = get_logger("company-update")
    settings = Settings()
    root = Path.cwd()

    if args.refresh:
        load_company_map(root=root, force_refresh=True, logger=logger)

    db_path = settings.stock_db_path or str(default_db_path(root))
    db = StockDB.open(db_path) if hasattr(StockDB, "open") else StockDB(db_path)

    logger.info("=== 公司基本資料更新開始 (db=%s) ===", db_path)
    count = backfill_stock_info(
        db, root=root, only_missing=not args.all, logger=logger,
    )
    logger.info("=== 完成：upsert %d 檔 ===", count)
    return 0


if __name__ == "__main__":
    sys.exit(main())

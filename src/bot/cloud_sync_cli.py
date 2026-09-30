"""stock-cloud-sync CLI -- Google Sheets 與本地 SQLite 雙向同步。

用法
====
    uv run stock-cloud-sync                  # 智能 sync 全部 SYNCABLE 表
    uv run stock-cloud-sync --push           # 本地 → Sheets
    uv run stock-cloud-sync --pull           # Sheets → 本地
    uv run stock-cloud-sync --row-level      # 依主鍵逐列合併雙向資料
    uv run stock-cloud-sync --tables llm_analysis_history,llm_daily_reports
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from bot.cloud_sync import (
    CloudSyncDependencyError,
    GoogleSheetSync,
    TableSyncResult,
    load_config_from_env,
)
from bot.cli_encoding import configure_console
from bot.stock_db import SYNCABLE_TABLES, StockDB, backup_stock_db, default_db_path
from bot.utils import get_logger


def _parse_tables(raw: str) -> List[str]:
    names = [t.strip() for t in raw.split(",") if t.strip()]
    unknown = [t for t in names if t not in SYNCABLE_TABLES]
    if unknown:
        raise ValueError(
            f"未知 table: {', '.join(unknown)}；"
            f"可用: {', '.join(SYNCABLE_TABLES)}"
        )
    return names


def _run_sync(
    sync: GoogleSheetSync,
    *,
    push: bool,
    pull: bool,
    tables: List[str],
    row_level: bool = True,
) -> List[TableSyncResult]:
    if push and pull:
        raise ValueError("不可同時指定 --push 與 --pull")
    if push:
        return sync.push_all(tables)
    if pull:
        return sync.pull_all(tables)
    return sync.sync_all(tables, row_level=row_level)


def _print_results(results: List[TableSyncResult]) -> None:
    ok = sum(1 for r in results if r.ok and not r.skipped)
    fail = sum(1 for r in results if not r.ok)
    print(f"\n完成：{ok} 成功 / {fail} 失敗 / {len(results)} 表\n")
    for r in results:
        status = "OK" if r.ok else "FAIL"
        if r.skipped:
            status = "SKIP"
        extra = r.note or r.error
        suffix = f" — {extra}" if extra else ""
        print(f"  [{status}] {r.table}: {r.direction}, rows={r.rows}{suffix}")


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(
        prog="stock-cloud-sync",
        description="同步本地 stock.db 與 Google Sheets (含 LLM 分析表)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--push", action="store_true", help="本地 → 雲端")
    mode.add_argument("--pull", action="store_true", help="雲端 → 本地")
    parser.add_argument(
        "--tables",
        default="",
        help=f"逗號分隔表名 (預設全部: {', '.join(SYNCABLE_TABLES)})",
    )
    parser.add_argument(
        "--row-level",
        action="store_true",
        dest="row_level",
        default=True,
        help="雙向同步時依主鍵逐列合併（預設啟用）",
    )
    parser.add_argument(
        "--table-level",
        action="store_false",
        dest="row_level",
        help="明確退回整表 updated_at 相容模式",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="專案資料根目錄（未指定時使用目前工作目錄）",
    )
    args = parser.parse_args(argv)

    project = args.root.resolve() if args.root else Path.cwd()
    logger = get_logger("cloud-sync", log_dir=project / "log")
    cfg = load_config_from_env(project)
    if not cfg.enabled:
        print(
            "雲端同步未啟用：請在 .env 設定 GOOGLE_SHEET_ID 與 GOOGLE_SA_JSON_PATH\n"
            "詳見 docs/cloud_sync_setup.md",
            file=sys.stderr,
        )
        return 2

    tables = _parse_tables(args.tables) if args.tables.strip() else list(SYNCABLE_TABLES)

    db = None
    try:
        db_path = default_db_path(project)
        if args.pull:
            try:
                bak = backup_stock_db(src=Path(db_path), label="pre-pull")
                logger.info("pull 前已備份 DB → %s", bak)
            except FileNotFoundError:
                logger.info("尚無本地 DB，略過 pull 前備份")

        db = StockDB.open(path=db_path)
        sync = GoogleSheetSync(cfg, db=db, logger=logger)
        results = _run_sync(
            sync,
            push=args.push,
            pull=args.pull,
            tables=tables,
            row_level=args.row_level,
        )
    except CloudSyncDependencyError as exc:
        print(f"錯誤: {exc}\n請執行: uv sync --extra cloud", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logger.exception("cloud-sync 失敗")
        print(f"錯誤: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()

    _print_results(results)
    if any(not r.ok for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

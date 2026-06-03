"""stock-cloud-sync CLI -- Google Sheets 與本地 SQLite 雙向同步。

用法
====
    uv run stock-cloud-sync                  # 智能 sync 全部 SYNCABLE 表
    uv run stock-cloud-sync --push           # 本地 → Sheets
    uv run stock-cloud-sync --pull           # Sheets → 本地
    uv run stock-cloud-sync --tables llm_analysis_history,llm_daily_reports
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from bot.cloud_sync import (
    CloudSyncDependencyError,
    GoogleSheetSync,
    TableSyncResult,
    load_config_from_env,
)
from bot.stock_db import SYNCABLE_TABLES, StockDB, default_db_path
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
) -> List[TableSyncResult]:
    if push and pull:
        raise ValueError("不可同時指定 --push 與 --pull")
    if push:
        return sync.push_all(tables)
    if pull:
        return sync.pull_all(tables)
    return sync.sync_all(tables)


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
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    logger = get_logger("cloud-sync")
    cfg = load_config_from_env()
    if not cfg.enabled:
        print(
            "雲端同步未啟用：請在 .env 設定 GOOGLE_SHEET_ID 與 GOOGLE_SA_JSON_PATH\n"
            "詳見 docs/cloud_sync_setup.md",
            file=sys.stderr,
        )
        return 2

    tables = _parse_tables(args.tables) if args.tables.strip() else list(SYNCABLE_TABLES)

    try:
        from bot.config import Settings
        settings = Settings()
        db_path = settings.stock_db_path or str(default_db_path())
        db = StockDB.open(db_path) if hasattr(StockDB, "open") else StockDB(db_path)
        sync = GoogleSheetSync(cfg, db=db, logger=logger)
        results = _run_sync(sync, push=args.push, pull=args.pull, tables=tables)
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

    _print_results(results)
    if any(not r.ok for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

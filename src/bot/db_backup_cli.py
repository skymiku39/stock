"""stock-db-backup CLI — 安全備份本地 SQLite。

用法
====
    uv run stock-db-backup
    uv run stock-db-backup --keep 30
    uv run stock-db-backup --label before-migrate --json
    uv run stock-db-backup --root C:\\skymiku\\stock --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from bot.stock_db import backup_stock_db, default_db_path
from bot.utils import get_logger


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-db-backup",
        description="用 SQLite online backup 複製 data/stock.db（或 STOCK_DB_PATH）",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="專案資料根目錄；未指定 --src 時從此根目錄解析 stock.db",
    )
    parser.add_argument(
        "--src",
        type=Path,
        default=None,
        help="來源 DB 路徑（相對路徑以 --root 為基準；預設 default_db_path(root)）",
    )
    parser.add_argument(
        "--dest-dir",
        type=Path,
        default=None,
        help="備份目錄（預設 <db 所在目錄>/backups）",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=14,
        help="保留最近 N 份 stock_*.db（預設 14；0=不清理）",
    )
    parser.add_argument(
        "--label",
        default="",
        help="檔名標籤，例如 pre-pull",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="輸出 JSON（給 CI / 腳本用）",
    )
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    project_root = (args.root or Path.cwd()).resolve()
    logger = get_logger("stock-db-backup", log_dir=project_root / "log")
    src = Path(args.src) if args.src is not None else default_db_path(project_root)
    if not src.is_absolute():
        src = project_root / src
    dest_dir = args.dest_dir
    if dest_dir is not None and not dest_dir.is_absolute():
        dest_dir = project_root / dest_dir
    try:
        dest = backup_stock_db(
            src=src,
            dest_dir=dest_dir,
            keep=args.keep,
            label=args.label,
        )
    except FileNotFoundError as exc:
        payload = {
            "ok": False,
            "command": "db-backup",
            "error": str(exc),
            "src": str(src),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"❌ {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logger.exception("備份失敗")
        if args.json:
            print(json.dumps({
                "ok": False,
                "command": "db-backup",
                "error": f"{type(exc).__name__}: {exc}",
                "src": str(src),
            }, ensure_ascii=False, indent=2))
        else:
            print(f"❌ 備份失敗: {exc}", file=sys.stderr)
        return 1

    size = dest.stat().st_size
    if args.json:
        print(json.dumps({
            "ok": True,
            "command": "db-backup",
            "artifacts": {"backup": str(dest)},
            "metrics": {"bytes": size},
            "src": str(src),
            "error": None,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"✅ 已備份\n  來源: {src}\n  備份: {dest}\n  大小: {size:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

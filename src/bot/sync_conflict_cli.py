"""stock-sync-conflicts -- 查詢與裁決本地 row-level 同步衝突。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from bot.stock_db import StockDB, default_db_path


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        prog="stock-sync-conflicts",
        description="查詢與裁決本地 row-level Google Sheets 同步衝突",
    )
    parser.add_argument("--root", type=Path, default=None, help="專案 root")
    parser.add_argument("--db", type=Path, default=None, help="SQLite 路徑")
    parser.add_argument("--table", default="", help="只查看指定 table")
    parser.add_argument(
        "--status", choices=("open", "resolved", "ignored", ""), default="open",
        help="篩選狀態；空字串代表全部",
    )
    parser.add_argument("--limit", type=int, default=100, help="最多列出幾筆")
    parser.add_argument("--resolve", default="", help="要裁決的 conflict_id")
    parser.add_argument("--resolution", default="", help="人工裁決說明")
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    args = parser.parse_args(argv)

    root = args.root.resolve() if args.root else Path.cwd()
    db_path = args.db
    if db_path is None:
        db_path = default_db_path(root)
    elif not db_path.is_absolute():
        db_path = root / db_path
    db = StockDB.open(path=db_path)
    try:
        if args.resolve:
            if not args.resolution.strip():
                print("--resolve 必須同時提供 --resolution", file=sys.stderr)
                return 2
            ok = db.resolve_sync_conflict(
                args.resolve,
                resolution=args.resolution,
            )
            payload = {
                "ok": ok,
                "conflict_id": args.resolve,
                "status": "resolved" if ok else "not_found",
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(json.dumps(payload, ensure_ascii=False))
            return 0 if ok else 1

        rows = db.list_sync_conflicts(
            args.table or None,
            status=args.status or None,
            limit=args.limit,
        )
        payload = [asdict(row) for row in rows]
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"共 {len(payload)} 筆同步衝突")
            for row in payload:
                print(
                    f"[{row['status']}] {row['conflict_id']} "
                    f"{row['table_name']} decision={row['decision']}"
                )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

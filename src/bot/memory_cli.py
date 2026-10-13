"""stock-memory -- 管理本地短期／長期記憶。

所有管理操作都保留事件；``delete`` 是軟刪除，不會直接抹掉原文。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from bot.memory_store import MemoryStore
from bot.stock_db import StockDB, default_db_path


def _db_from_args(args: argparse.Namespace) -> StockDB:
    if args.db:
        db_path = Path(args.db)
        if not db_path.is_absolute():
            root = (args.root or Path.cwd()).resolve()
            db_path = root / db_path
        return StockDB.open(path=db_path)
    return StockDB.open(path=default_db_path(args.root))


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv: Optional[List[str]] = None) -> int:
    # Configure stdout before argparse handles --help; otherwise Windows can
    # render the Traditional Chinese descriptions with the legacy console code
    # page and hide an otherwise valid CLI contract behind mojibake.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="管理本地短期／長期記憶")
    parser.add_argument("--db", default="", help="SQLite 路徑")
    parser.add_argument("--root", type=Path, default=None, help="專案 root（未指定 --db 時使用）")
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="搜尋記憶")
    search.add_argument("query")
    search.add_argument("--layer", choices=("short", "long", "both"), default="both")
    search.add_argument("--scope-type", default="")
    search.add_argument("--scope-id", default="")
    search.add_argument("--include-low-quality", action="store_true")
    search.add_argument("--limit", type=int, default=20)

    listing = sub.add_parser("list", help="列出記憶")
    listing.add_argument("--layer", choices=("short", "long"), default="")
    listing.add_argument("--status", default="active")
    listing.add_argument("--scope-type", default="")
    listing.add_argument("--scope-id", default="")
    listing.add_argument("--limit", type=int, default=50)

    sub.add_parser("stats", help="顯示記憶統計")
    # argparse treats '%' in help strings as an interpolation marker.
    sub.add_parser("capacity", help="顯示短期／長期容量與 80%% 警戒")
    sub.add_parser("archive-expired", help="封存已過期短期記憶")

    backfill = sub.add_parser("backfill-llm", help="將既有 LLM JSONL 補進短期記憶")
    backfill.add_argument("--log-dir", type=Path, default=None)
    backfill.add_argument("--limit", type=int, default=100_000)
    sub.add_parser("backfill-db", help="將既有 SQLite LLM 索引補進長期記憶")

    promote = sub.add_parser("promote", help="將短期記憶升級為長期記憶")
    promote.add_argument("memory_id")
    promote.add_argument("--summary", default="")

    propose = sub.add_parser("propose", help="建立待管理者審核的記憶提案")
    propose.add_argument("--content", required=True)
    propose.add_argument("--summary", default="")
    propose.add_argument("--proposal-type", choices=("create", "update", "delete", "consolidate"), default="create")
    propose.add_argument(
        "--target-memory-id",
        default="",
        help="update/delete 的目標 ID；consolidate 可填逗號分隔的子記憶 ID",
    )
    propose.add_argument("--layer", choices=("short", "long"), default="long")
    propose.add_argument("--memory-type", default="summary")
    propose.add_argument("--scope-type", default="global")
    propose.add_argument("--scope-id", default="")
    propose.add_argument("--subject", default="")
    propose.add_argument("--keywords", default="")
    propose.add_argument("--importance", type=float, default=0.5)
    propose.add_argument("--confidence", type=float, default=0.0)
    propose.add_argument("--quality-status", default="unknown")
    propose.add_argument("--source", default="manual")
    propose.add_argument("--source-ref", default="")
    propose.add_argument("--evidence-json", default="[]", help="證據 packet JSON 陣列")
    propose.add_argument("--metadata-json", default="{}")
    propose.add_argument("--proposer", default="memory_cli")

    proposals = sub.add_parser("list-proposals", help="列出記憶提案")
    proposals.add_argument("--status", default="pending", help="pending/approved/rejected/superseded；空字串代表全部")
    proposals.add_argument("--limit", type=int, default=50)

    evidence = sub.add_parser("evidence", help="查詢中央證據登錄")
    evidence.add_argument("--memory-id", default="")
    evidence.add_argument("--source", default="")
    evidence.add_argument("--limit", type=int, default=50)

    approve = sub.add_parser("approve", help="核准記憶提案並寫入記憶")
    approve.add_argument("proposal_id")
    approve.add_argument("--reviewer", default="memory_cli")
    approve.add_argument("--note", default="")

    reject = sub.add_parser("reject", help="拒絕記憶提案並保留稽核紀錄")
    reject.add_argument("proposal_id")
    reject.add_argument("--reviewer", default="memory_cli")
    reject.add_argument("--note", default="")

    delete = sub.add_parser("delete", help="軟刪除記憶並保留稽核紀錄")
    delete.add_argument("memory_id")

    args = parser.parse_args(argv)
    db = _db_from_args(args)
    store = MemoryStore(db, actor="memory_cli")

    try:
        if args.command == "search":
            layers = ("short", "long") if args.layer == "both" else (args.layer,)
            rows = store.search(
                args.query,
                layers=layers,
                scope_type=args.scope_type or None,
                scope_id=args.scope_id if args.scope_id else None,
                include_low_quality=args.include_low_quality,
                limit=args.limit,
            )
            _print_json([asdict(row) for row in rows])
        elif args.command == "list":
            rows = db.list_memory_items(
                memory_layer=args.layer or None,
                memory_status=args.status or None,
                scope_type=args.scope_type or None,
                scope_id=args.scope_id if args.scope_id else None,
                limit=args.limit,
            )
            _print_json([asdict(row) for row in rows])
        elif args.command == "stats":
            _print_json(store.stats())
        elif args.command == "capacity":
            _print_json(store.capacity_report())
        elif args.command == "archive-expired":
            _print_json({"archived": store.archive_expired(), "stats": store.stats()})
        elif args.command == "backfill-llm":
            count = store.backfill_llm_logs(args.log_dir, limit=args.limit)
            _print_json({"backfilled": count, "stats": store.stats()})
        elif args.command == "backfill-db":
            _print_json({"backfilled": store.backfill_database(), "stats": store.stats()})
        elif args.command == "promote":
            outcome = store.promote(args.memory_id, summary=args.summary)
            if not outcome.ok:
                detail = "; ".join(outcome.reasons) or "找不到可升級的 active short memory"
                print(f"拒絕升級: {detail}", file=sys.stderr)
                _print_json(outcome.to_dict())
                return 1
            _print_json(outcome.to_dict())
        elif args.command == "propose":
            try:
                evidence = json.loads(args.evidence_json or "[]")
                metadata = json.loads(args.metadata_json or "{}")
            except json.JSONDecodeError as exc:
                print(f"JSON 格式錯誤: {exc}", file=sys.stderr)
                return 2
            row = store.propose(
                args.content,
                proposal_type=args.proposal_type,
                target_memory_id=args.target_memory_id,
                memory_layer=args.layer,
                memory_type=args.memory_type,
                scope_type=args.scope_type,
                scope_id=args.scope_id,
                subject=args.subject,
                summary=args.summary,
                keywords=args.keywords,
                importance=args.importance,
                confidence=args.confidence,
                quality_status=args.quality_status,
                source=args.source,
                source_ref=args.source_ref,
                evidence=evidence,
                metadata=metadata,
                proposer=args.proposer,
            )
            _print_json(asdict(row))
        elif args.command == "list-proposals":
            rows = db.list_memory_proposals(status=args.status or None, limit=args.limit)
            _print_json([asdict(row) for row in rows])
        elif args.command == "evidence":
            rows = db.list_evidence(
                memory_id=args.memory_id or None,
                source=args.source or None,
                limit=args.limit,
            )
            _print_json([asdict(row) for row in rows])
        elif args.command == "approve":
            row = store.approve_proposal(
                args.proposal_id, reviewer=args.reviewer, review_note=args.note,
            )
            if row is None:
                print("找不到 pending 提案，或提案無法核准", file=sys.stderr)
                return 1
            _print_json(asdict(row))
        elif args.command == "reject":
            if not store.reject_proposal(
                args.proposal_id, reviewer=args.reviewer, review_note=args.note,
            ):
                print("找不到 pending 提案", file=sys.stderr)
                return 1
            _print_json({"proposal_id": args.proposal_id, "status": "rejected"})
        elif args.command == "delete":
            if not store.delete(args.memory_id):
                print("找不到記憶", file=sys.stderr)
                return 1
            _print_json({"deleted": args.memory_id, "mode": "soft-delete"})
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

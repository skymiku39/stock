"""Safe CLI for the explicit research quarantine primitive.

Default behaviour is read-only / dry-run.  ``--apply`` requires a concrete
``--source-document-id`` and never deletes raw bytes or relational rows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.cli_encoding import configure_console
from bot.research_repair import (
    list_quarantined_research_chains,
    plan_quarantine_research_chain,
    quarantine_research_chain,
)


def _print_report(report: Dict[str, Any]) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(
        prog="stock-research-quarantine",
        description=(
            "隔離已知錯誤的研究衍生鏈；預設只 dry-run／列出，"
            "不會刪除 raw 或資料列，也不會猜測 provenance。"
        ),
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出已隔離的 source／衍生鏈（唯讀）",
    )
    parser.add_argument(
        "--source-document-id",
        default="",
        help="精確的 source_document_id；--apply 時必填",
    )
    parser.add_argument(
        "--replacement-source-document-id",
        default="",
        help="可選；必須已存在，不會自動猜測或建立",
    )
    parser.add_argument(
        "--reason",
        default="",
        help="隔離原因；--apply 時必填",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="對指定 source_document_id 實際寫入隔離標記；預設只 dry-run",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    source_id = str(args.source_document_id or "").strip()
    replacement_id = str(args.replacement_source_document_id or "").strip()
    reason = str(args.reason or "").strip()

    if args.list:
        _print_report(list_quarantined_research_chains(root=root))
        return 0

    if args.apply:
        if not source_id:
            _print_report({
                "ok": False,
                "dry_run": False,
                "command": "research_quarantine",
                "status": "refused",
                "error": (
                    "--apply requires an explicit --source-document-id; "
                    "refusing to quarantine without concrete scope"
                ),
            })
            return 2
        if not reason:
            _print_report({
                "ok": False,
                "dry_run": False,
                "command": "research_quarantine",
                "status": "refused",
                "error": "--apply requires --reason",
                "source_document_id": source_id,
            })
            return 2
        try:
            report = quarantine_research_chain(
                root=root,
                source_document_id=source_id,
                replacement_source_document_id=replacement_id,
                reason=reason,
            )
        except ValueError as exc:
            _print_report({
                "ok": False,
                "dry_run": False,
                "command": "research_quarantine",
                "status": "error",
                "error": str(exc),
                "source_document_id": source_id,
            })
            return 1
        _print_report({"ok": True, "command": "research_quarantine", **report})
        return 0

    if source_id:
        try:
            report = plan_quarantine_research_chain(
                root=root,
                source_document_id=source_id,
                replacement_source_document_id=replacement_id,
                reason=reason,
            )
        except ValueError as exc:
            _print_report({
                "ok": False,
                "dry_run": True,
                "command": "research_quarantine",
                "status": "error",
                "error": str(exc),
                "source_document_id": source_id,
            })
            return 1
        _print_report({"ok": True, "command": "research_quarantine", **report})
        return 0

    _print_report({
        "ok": True,
        "dry_run": True,
        "command": "research_quarantine",
        "status": "dry_run",
        "message": (
            "read-only default; pass --list or --source-document-id to preview, "
            "and --apply only with an explicit --source-document-id"
        ),
        "source_document_id": "",
        "research_document_ids": [],
        "evidence_ids": [],
        "claim_ids": [],
        "llm_run_ids": [],
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]



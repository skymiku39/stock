"""Exact-only repair CLI for historical evidence source links."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from bot.cli_encoding import configure_console
from bot.research_repair import repair_evidence_source_links


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(
        prog="stock-evidence-link-repair",
        description=(
            "以 exact metadata 補回 evidence 與 source_document link；"
            "預設只 dry-run，不修改資料。"
        ),
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--limit", type=int, default=100_000)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="實際新增可證明的 source links；不會刪除或修改 raw／claim",
    )
    args = parser.parse_args(argv)
    report = repair_evidence_source_links(
        root=args.root.resolve(),
        apply=bool(args.apply),
        limit=max(0, int(args.limit)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]

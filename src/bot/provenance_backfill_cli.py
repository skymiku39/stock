"""CLI for exact-only LLM provenance backfill."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

from bot.cli_encoding import configure_console
from bot.provenance_backfill import backfill_llm_provenance


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="回填可證明的 LLM research provenance")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--limit", type=int, default=100_000)
    parser.add_argument(
        "--apply", action="store_true",
        help="實際寫入 relational research tables；未指定時只 dry-run",
    )
    args = parser.parse_args(argv)
    report = backfill_llm_provenance(
        root=args.root, apply=args.apply, limit=max(0, args.limit),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

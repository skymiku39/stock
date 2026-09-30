"""``stock-system-preflight`` -- 本地研究／證據／記憶整合唯讀檢查。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import List, Optional

from bot.cli_encoding import configure_console
from bot.config import Settings
from bot.system_preflight import report_to_dict, run_system_preflight


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(
        prog="stock-system-preflight",
        description="唯讀檢查 SQLite、raw、research、evidence、LLM 與可選雲端設定",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--imports", action="store_true", help="逐一 import 所有 bot 模組")
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    # Optional dashboard/market packages can log deprecation notices while
    # being imported.  Keep --json a strict JSON stream for CI and schedulers.
    with contextlib.ExitStack() as stack:
        if args.json and args.imports:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        settings = Settings(_env_file=str(root / ".env"))  # type: ignore[call-arg]
        report = run_system_preflight(
            root,
            settings=settings,
            check_imports=args.imports,
        )
    if args.json:
        print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2, default=str))
    else:
        print(f"=== System preflight ({report.fetched_at}) ===")
        print(report.summary)
        for item in report.checks:
            print(f"[{item.status.upper()}] {item.name}: {item.detail}")
            if item.suggestion:
                print(f"  -> {item.suggestion}")
    return 2 if report.status == "blocked" else 0


if __name__ == "__main__":
    sys.exit(main())

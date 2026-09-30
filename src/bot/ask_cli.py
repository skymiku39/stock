"""stock-ask -- 本地優先的 ``!ask`` 命令列入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from bot.ask import AskEngine
from bot.config import Settings
from bot.llm_analyzer import DEFAULT_MODEL, GeminiClient
from bot.utils import log_root


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="QueryPlan + EvidencePack 本地問答")
    parser.add_argument("query", nargs="?", help="要詢問的問題")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--ticker", default="")
    parser.add_argument("--scope-type", default="global")
    parser.add_argument("--scope-id", default="")
    parser.add_argument("--no-llm", action="store_true", help="只做規劃與證據檢索")
    parser.add_argument("--live", action="store_true", help="啟用條件式即時來源")
    parser.add_argument("--follow-up", default="", help="沿用既有 trace_id 補抓待取得來源")
    parser.add_argument("--json", action="store_true", help="輸出完整 JSON")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    project_root = args.root.resolve()
    with log_root(project_root):
        client = None
        if not args.no_llm:
            settings = Settings(_env_file=str(project_root / ".env"))  # type: ignore[call-arg]
            if settings.llm_enabled:
                client = GeminiClient(
                    api_key=settings.gemini_api_key,
                    model=settings.gemini_model or DEFAULT_MODEL,
                    settings=settings,
                    root=project_root,
                )

        engine = AskEngine(
            root=project_root,
            client=client,
            enable_live_sources=True if args.live else None,
        )
        try:
            if args.follow_up:
                pack = engine.follow_up(args.follow_up)
                if args.json:
                    print(json.dumps(pack.to_dict(), ensure_ascii=False, indent=2, default=str))
                else:
                    print(json.dumps(pack.to_dict(), ensure_ascii=False, indent=2, default=str))
                return 0
            if not args.query:
                parser.error("需要 query，或使用 --follow-up TRACE_ID")
            result = engine.ask(
                args.query,
                ticker=args.ticker,
                scope_type=args.scope_type,
                scope_id=args.scope_id,
            )
            if args.json:
                print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
            else:
                print(result.answer)
                print(f"status={result.status} trace_id={result.trace_id}")
                print(f"evidence={len(result.evidence.usable_items)} plan={result.plan.plan_id}")
                if result.verification.errors:
                    print("verification=" + "; ".join(result.verification.errors))
            return 0
        finally:
            engine.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Small CLI for verifying the Gemini API setup.

Delegates to the unified llm_smoke CLI with --provider gemini.
"""

from __future__ import annotations

import sys

from bot.llm_smoke import main as llm_smoke_main


def main(argv: list[str] | None = None) -> int:
    """向後相容入口：強制 --provider gemini。"""
    if argv is None:
        args = ["--provider", "gemini"] + sys.argv[1:]
    else:
        args = list(argv)
        if "--provider" not in args:
            args = ["--provider", "gemini"] + args
    return llm_smoke_main(args)


if __name__ == "__main__":
    sys.exit(main())

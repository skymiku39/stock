"""``stock-test`` -- unit / regression pytest runner (subprocess)."""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

UNIT_MARKER_EXPR = "not integration and not slow"


def _split_passthrough(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split CLI args and pytest passthrough after ``--`` (order preserved)."""
    raw = list(argv)
    if "--" in raw:
        idx = raw.index("--")
        return raw[:idx], raw[idx + 1 :]
    return raw, []


def _validate_root(root: Path) -> Path:
    """Resolve and validate a project root (must exist, be a dir, contain tests/)."""
    resolved = root.expanduser().resolve()
    if not resolved.exists():
        print(f"error: --root does not exist: {resolved}", file=sys.stderr)
        raise SystemExit(2)
    if not resolved.is_dir():
        print(f"error: --root is not a directory: {resolved}", file=sys.stderr)
        raise SystemExit(2)
    tests_dir = resolved / "tests"
    if not tests_dir.is_dir():
        print(
            f"error: --root is not a valid project root (missing tests/): {resolved}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return resolved


def _has_marker_override(pytest_args: Sequence[str]) -> bool:
    """Return True if passthrough would override marker expression.

    Detects ``-m`` / ``--markexpr`` (with or without ``=``) and attached short
    forms such as ``-mslow`` / ``-mintegration``.
    """
    for arg in pytest_args:
        if arg in ("-m", "--markexpr"):
            return True
        if arg.startswith("--markexpr="):
            return True
        # Attached short form: -mslow, -mintegration, -m"not slow", …
        if arg.startswith("-m") and not arg.startswith("--"):
            return True
    return False


def _pytest_installed() -> bool:
    """Detect pytest without importing it (help path must not need pytest)."""
    return importlib.util.find_spec("pytest") is not None


def build_pytest_argv(profile: str, root: Path, pytest_args: Sequence[str]) -> list[str]:
    """Build argv for ``python -m pytest`` (excluding the executable)."""
    tests_path = str(root / "tests")
    argv = ["-m", "pytest", tests_path]
    if profile == "unit":
        if _has_marker_override(pytest_args):
            print(
                "error: unit profile rejects -m/--markexpr passthrough; "
                "it would silently defeat "
                f'-m "{UNIT_MARKER_EXPR}". '
                "Use regression, or invoke pytest directly for custom markers.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        argv.extend(["-m", UNIT_MARKER_EXPR])
    # regression: full tests/ discovery, no marker filter
    argv.extend(pytest_args)
    return argv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-test",
        description=(
            "Run project tests via pytest subprocess. "
            "unit: -m \"not integration and not slow\". "
            "regression: complete tests/ discovery. "
            "Pass extra pytest args after -- ."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root (default: current working directory). Must contain tests/.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "unit",
        help=f'Fast profile: pytest tests/ -m "{UNIT_MARKER_EXPR}"',
    )
    sub.add_parser(
        "regression",
        help="Full profile: pytest tests/ (no marker filter)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    cli_argv, pytest_args = _split_passthrough(raw)
    parser = _build_parser()
    args = parser.parse_args(cli_argv)

    root = _validate_root(args.root if args.root is not None else Path.cwd())

    if not _pytest_installed():
        print(
            "error: pytest is not installed. "
            "Install dev dependencies with: uv sync --extra dev",
            file=sys.stderr,
        )
        return 1

    pytest_argv = build_pytest_argv(args.command, root, pytest_args)
    cmd = [sys.executable, *pytest_argv]
    completed = subprocess.run(cmd, cwd=root)
    return int(completed.returncode)


if __name__ == "__main__":
    sys.exit(main())

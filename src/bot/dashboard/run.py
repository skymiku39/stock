"""Console entry: spawn streamlit."""
from __future__ import annotations

import subprocess
import sys
import argparse
import os
from pathlib import Path

from bot.cli_encoding import configure_console


def run() -> None:
    configure_console()
    parser = argparse.ArgumentParser(
        prog="stock-dashboard",
        description="啟動 Stock Bot Streamlit 儀表板",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="專案 root；未指定時使用目前安裝的專案根目錄",
    )
    known, extra = parser.parse_known_args(sys.argv[1:])
    if known.root is not None:
        os.environ["STOCK_DASHBOARD_ROOT"] = str(known.root.resolve())

    from bot.dashboard.common import DASHBOARD_UI_BUILD

    print(f"[stock-dashboard] UI build: {DASHBOARD_UI_BUILD}", flush=True)
    script = Path(__file__).resolve().parent / "nav.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(script)]
    if extra:
        # `--` is Streamlit's separator for arguments intended for the app
        # script.  The wrapper's extra arguments are Streamlit server flags
        # (for example `--server.port 8791`), so pass them directly; placing
        # them after the separator silently leaves Streamlit on port 8501.
        cmd.extend(extra)
    try:
        sys.exit(subprocess.call(cmd))
    except KeyboardInterrupt:
        sys.exit(0)

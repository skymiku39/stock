"""Console entry: spawn streamlit."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run() -> None:
    from bot.dashboard.common import DASHBOARD_UI_BUILD

    print(f"[stock-dashboard] UI build: {DASHBOARD_UI_BUILD}", flush=True)
    script = Path(__file__).resolve().parent / "nav.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(script)]
    extra = sys.argv[1:]
    if extra:
        cmd.append("--")
        cmd.extend(extra)
    try:
        sys.exit(subprocess.call(cmd))
    except KeyboardInterrupt:
        sys.exit(0)

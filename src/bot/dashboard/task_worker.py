"""Private process entry point for one already-claimed dashboard task."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from bot.dashboard.task_service import TaskService


def _watch_parent():
    # The supervisor never writes to stdin. EOF means it exited or crashed.
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            # A blocking CRT stdin read can hold the descriptor lock while
            # NumPy loads native libraries. Peek at the pipe without reading.
            peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
            peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                             ctypes.POINTER(wintypes.DWORD)]
            peek.restype = wintypes.BOOL
            handle = msvcrt.get_osfhandle(sys.stdin.fileno())
            while peek(handle, None, 0, None, None, None):
                time.sleep(0.2)
        else:
            import select
            select.select([sys.stdin.fileno()], [], [])
    finally:
        _stop_orphan()


def _stop_orphan():
    try:
        if os.name == "nt":
            subprocess.run(["taskkill.exe", "/PID", str(os.getpid()), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        elif os.getpgrp() == os.getpid():
            os.killpg(os.getpgrp(), signal.SIGKILL)
    finally:
        os._exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    threading.Thread(target=_watch_parent, daemon=True).start()
    service = TaskService(args.root, start=False)
    service.owner = args.owner
    with service._connect() as db:
        row = db.execute("SELECT * FROM tasks WHERE id=? AND owner=? AND state='running'",
                         (args.task_id, args.owner)).fetchone()
    if row is None:
        return 2
    outcome = service._execute(service._record(row))
    # Only the supervisor can publish a terminal state, after the worker exits.
    args.result.write_text(json.dumps(outcome, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

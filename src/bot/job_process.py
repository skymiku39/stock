"""Bounded subprocess execution for scheduled jobs."""

from __future__ import annotations

import os
import signal
import subprocess


def run_with_deadline(cmd, *, cwd, env, stdout, timeout: float) -> int:
    """Wait with a deadline and terminate the owned process tree on timeout."""
    if timeout <= 0:
        raise ValueError("Job timeout must be positive")
    kwargs = dict(cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                cleanup = subprocess.run(
                    ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if cleanup.returncode != 0 and proc.poll() is None:
                    raise RuntimeError("Timed out; process tree termination failed")
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        raise

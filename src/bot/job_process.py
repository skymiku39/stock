"""Bounded subprocess execution for scheduled jobs."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import time


class ProcessCancelled(RuntimeError):
    """The owner requested shutdown; the child tree has been terminated."""


class ProcessTerminationError(RuntimeError):
    """Termination could not be confirmed; callers must not release the queue."""


def run_with_deadline(cmd, *, cwd, env, stdout, timeout: float,
                      cancel_event=None, guard_parent: bool = False, on_start=None) -> int:
    """Wait with a deadline and terminate the owned process tree on timeout."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Job timeout must be positive")
    if cancel_event is not None and cancel_event.is_set():
        raise ProcessCancelled("Service stopped before launch")
    kwargs = dict(cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT)
    if guard_parent:
        # The dashboard worker watches EOF on this pipe to stop if its owner
        # crashes. Keep the write end open for the entire child lifetime.
        kwargs["stdin"] = subprocess.PIPE
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        if on_start is not None:
            on_start(proc)
        if cancel_event is None:
            return proc.wait(timeout=timeout)
        deadline = time.monotonic() + timeout
        while True:
            if cancel_event.is_set():
                raise ProcessCancelled("Service stopped during execution")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                return proc.wait(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                pass
    except (subprocess.TimeoutExpired, ProcessCancelled):
        try:
            terminate_process_tree(proc)
        except Exception as exc:
            raise ProcessTerminationError(f"Cannot confirm termination of PID {proc.pid}: {exc}") from exc
        raise
    finally:
        if proc.stdin is not None:
            proc.stdin.close()


def process_created_at(pid):
    import datetime as dt
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                return None
            created = ((values[0].dwHighDateTime << 32) + values[0].dwLowDateTime) / 10000000 - 11644473600
            return dt.datetime.fromtimestamp(created, dt.timezone.utc).isoformat()
        finally:
            kernel.CloseHandle(handle)
    return dt.datetime.now(dt.timezone.utc).isoformat()


def terminate_process_tree(proc):
    """Terminate only the owned process group/tree and reap its leader."""
    try:
        if os.name == "nt":
            cleanup = subprocess.run(
                ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if cleanup.returncode != 0 and proc.poll() is None:
                raise RuntimeError("Process tree termination failed")
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)

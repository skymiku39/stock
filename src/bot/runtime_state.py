"""Small durable state primitives shared by independent background processes."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid


class StateBusy(RuntimeError):
    pass


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        if not self.acquire():
            raise StateBusy(str(self.path))
        return self

    def __exit__(self, *args):
        self.release()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {} if default is None else default


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_text(value, encoding='utf-8')
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.05 * (attempt + 1))
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def code_identity(root):
    root = Path(root)
    digests = {}
    for folder, pattern in (('src/bot', '*.py'), ('scripts', '*.ps1'), ('prompts', '*.yaml')):
        for path in sorted((root / folder).rglob(pattern)):
            digests[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True,
                                       timeout=5, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        head = None
    return {'git_head': head, 'source_sha256': hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest(),
            'files': digests}

"""cloud_file_cache -- opt-in file mirror for fetched cache artifacts.

This module mirrors files under the project (usually ``data/...``) into a
Google Drive-synced folder configured by ``GOOGLE_CACHE_DIR``.  It is purposely
simple: local SQLite/CSV/JSON remains the read path, while the cloud folder acts
as a cross-machine cache seed.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

ENV_CACHE_DIR = "GOOGLE_CACHE_DIR"


def cloud_cache_root(root: Path | None = None) -> Path | None:
    """Return the configured Google cache directory, if enabled."""
    value = (os.getenv(ENV_CACHE_DIR) or "").strip().strip('"').strip("'")
    if not value:
        value = _dotenv_value(ENV_CACHE_DIR, root=root)
    if not value:
        return None
    base = _project_root(root)
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = base / p
    return p


def cloud_path_for(local_path: Path, root: Path | None = None) -> Path | None:
    """Map a local project path to its cloud-cache mirror path."""
    cloud_root = cloud_cache_root(root=root)
    if cloud_root is None:
        return None
    local = Path(local_path)
    base = _base_for(local, root=root)
    try:
        rel = local.resolve().relative_to(base)
    except Exception:
        rel = Path(local.name)
    return cloud_root / rel


def restore_file_from_cloud(
    local_path: Path,
    *,
    root: Path | None = None,
    prefer_newer: bool = True,
) -> bool:
    """Copy a mirrored file back to local storage when missing or newer."""
    local = Path(local_path)
    cloud = cloud_path_for(local, root=root)
    if cloud is None or not cloud.exists() or cloud.is_dir():
        return False
    if _same_path(local, cloud):
        return False
    if local.exists() and prefer_newer:
        try:
            if cloud.stat().st_mtime <= local.stat().st_mtime:
                return False
        except OSError:
            return False
    local.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cloud, local)
    return True


def restore_tree_from_cloud(
    local_dir: Path,
    *,
    root: Path | None = None,
    prefer_newer: bool = True,
) -> int:
    """Restore all files from a mirrored cloud directory."""
    local = Path(local_dir)
    cloud = cloud_path_for(local, root=root)
    if cloud is None or not cloud.exists() or not cloud.is_dir():
        return 0
    copied = 0
    for src in cloud.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(cloud)
        dst = local / rel
        if _same_path(src, dst):
            continue
        if dst.exists() and prefer_newer:
            try:
                if src.stat().st_mtime <= dst.stat().st_mtime:
                    continue
            except OSError:
                continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied


def mirror_file_to_cloud(local_path: Path, *, root: Path | None = None) -> bool:
    """Copy a local file into the configured Google cache directory."""
    local = Path(local_path)
    if not local.exists() or local.is_dir():
        return False
    cloud = cloud_path_for(local, root=root)
    if cloud is None or _same_path(local, cloud):
        return False
    cloud.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local, cloud)
    return True


def read_json_cache(
    path: Path,
    *,
    root: Path | None = None,
    ttl_seconds: int | None = None,
) -> Any | None:
    """Read a JSON cache, restoring from the cloud mirror first when useful."""
    p = Path(path)
    restore_file_from_cloud(p, root=root)
    if not p.exists():
        return None
    if ttl_seconds is not None:
        try:
            if time.time() - p.stat().st_mtime > ttl_seconds:
                return None
        except OSError:
            return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json_cache(
    path: Path,
    data: Any,
    *,
    root: Path | None = None,
    indent: int | None = None,
) -> Path:
    """Write JSON locally and mirror it to the cloud cache when configured."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )
    mirror_file_to_cloud(p, root=root)
    return p


def _dotenv_value(key: str, *, root: Path | None = None) -> str:
    path = _project_root(root) / ".env"
    if not path.exists():
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


def _project_root(root: Path | None = None) -> Path:
    return Path(root or Path.cwd()).resolve()


def _base_for(local_path: Path, *, root: Path | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    try:
        resolved = Path(local_path).resolve()
        parts = resolved.parts
        for i, part in enumerate(parts):
            if part == "data" and i > 0:
                return Path(*parts[:i]).resolve()
    except Exception:
        pass
    return _project_root(root)


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except Exception:
        return False


__all__ = [
    "ENV_CACHE_DIR",
    "cloud_cache_root",
    "cloud_path_for",
    "mirror_file_to_cloud",
    "read_json_cache",
    "restore_file_from_cloud",
    "restore_tree_from_cloud",
    "write_json_cache",
]

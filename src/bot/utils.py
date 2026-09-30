from __future__ import annotations

import datetime
import logging
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator, Optional, Union
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")
_LOG_DIR_CONTEXT: ContextVar[Optional[Path]] = ContextVar(
    "streamsuite_stock_log_dir", default=None,
)


@contextmanager
def log_root(root: Union[str, os.PathLike[str]]) -> Iterator[Path]:
    """在一段執行期間把預設 logger 固定到指定專案的 ``log``。"""
    log_dir = Path(root).expanduser().resolve() / "log"
    token = _LOG_DIR_CONTEXT.set(log_dir)
    try:
        yield log_dir
    finally:
        _LOG_DIR_CONTEXT.reset(token)


def configure_utf8_stdio() -> None:
    """讓 Windows CLI 的 stdout/stderr 優先以 UTF-8 輸出。

    檔案與 SQLite 本來就以 UTF-8 保存；若只修資料層而不處理主控台，
    中文 log 仍可能在 Windows code page 下顯示為亂碼。某些嵌入式 runner
    沒有 ``reconfigure``，因此這裡必須是 best-effort，不能阻斷主流程。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            continue


def now_tw() -> datetime.datetime:
    """回傳台灣時區的當前時間。"""
    return datetime.datetime.now(TW_TZ)


def now_tw_time() -> datetime.time:
    """回傳台灣時區的當前 time (不含日期)。"""
    return now_tw().time()


def mk_folder(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def get_logger(
    name: str = "bot",
    log_dir: Optional[Union[str, os.PathLike[str]]] = None,
    log_level: int = logging.DEBUG,
) -> logging.Logger:
    configure_utf8_stdio()
    selected_log_dir = (
        Path(log_dir).expanduser()
        if log_dir is not None
        else (_LOG_DIR_CONTEXT.get() or Path("log"))
    ).resolve()
    mk_folder(str(selected_log_dir))

    date_str = now_tw().strftime("%Y-%m-%d")
    logger = logging.getLogger(name)
    logger.setLevel(log_level)

    desired_log_path = selected_log_dir / f"{name}_{date_str}.log"
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                if Path(handler.baseFilename).resolve() == desired_log_path:
                    return logger
            except (OSError, ValueError):
                continue

    # A long-lived process may switch project roots.  Rebuild only when the
    # existing logger points at another file; this avoids silently retaining
    # a previous project's log path.
    if logger.handlers:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    formatter = logging.Formatter(
        "[%(asctime)s %(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(
        str(desired_log_path),
        encoding="utf-8",
    )
    fh.setLevel(log_level)
    fh.setFormatter(formatter)

    sh = logging.StreamHandler()
    sh.setLevel(log_level)
    sh.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(sh)

    return logger

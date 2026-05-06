from __future__ import annotations

import datetime
import logging
import os
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")


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
    log_dir: str = "log",
    log_level: int = logging.DEBUG,
) -> logging.Logger:
    mk_folder(log_dir)

    date_str = now_tw().strftime("%Y-%m-%d")
    logger = logging.getLogger(name)
    logger.setLevel(log_level)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "[%(asctime)s %(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(
        os.path.join(log_dir, f"{name}_{date_str}.log"),
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

from __future__ import annotations

import logging
from pathlib import Path

from bot.utils import get_logger, log_root, now_tw


def test_default_logger_uses_active_project_log_root(tmp_path: Path) -> None:
    logger_name = "root-isolation-test"
    logger = logging.getLogger(logger_name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    with log_root(tmp_path):
        logger = get_logger(logger_name)
        logger.info("root scoped log")

    expected = (
        tmp_path / "log" / f"root-isolation-test_{now_tw():%Y-%m-%d}.log"
    ).resolve()
    file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert file_handlers
    assert Path(file_handlers[0].baseFilename).resolve() == expected
    assert expected.is_file()

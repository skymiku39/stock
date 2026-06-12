"""共用 pytest fixtures。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.risk_guard import RiskGuard


@pytest.fixture(autouse=True)
def _isolate_risk_guard_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """避免測試讀寫專案 data/ 內真實 risk_state。"""
    original_init = RiskGuard.__init__

    def _init(
        self,
        settings,
        project_root: Path | None = None,
        logger=None,
    ) -> None:
        original_init(
            self,
            settings,
            project_root=project_root or tmp_path,
            logger=logger,
        )

    monkeypatch.setattr(RiskGuard, "__init__", _init)

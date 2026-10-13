"""共用 pytest fixtures。"""

from __future__ import annotations

from pathlib import Path
import importlib
import socket

import pytest

from bot.risk_guard import RiskGuard


def pytest_addoption(parser):
    parser.addoption("--allow-network", action="store_true", default=False,
                     help="Allow real network only in tests marked integration")


@pytest.fixture(autouse=True)
def _unit_tests_are_offline(request, monkeypatch):
    """Missing mocks fail immediately instead of calling APIs or hiding fallbacks."""
    if request.node.get_closest_marker("integration"):
        if not request.config.getoption("--allow-network"):
            pytest.skip("Real integration requires explicit --allow-network")
        yield
        return
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append(True)
        pytest.fail("Unit test attempted real network; mock the source or mark it integration")

    async def async_blocked(*args, **kwargs):
        blocked()

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    # curl_cffi performs connections in native code and bypasses Python sockets.
    for module_name, class_name, method_name, replacement in (
        ("requests.sessions", "Session", "request", blocked),
        ("curl_cffi.requests.session", "Session", "request", blocked),
        ("curl_cffi.requests.session", "AsyncSession", "request", async_blocked),
        ("httpx", "Client", "send", blocked),
        ("httpx", "AsyncClient", "send", async_blocked),
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        monkeypatch.setattr(getattr(module, class_name), method_name, replacement)
    yield
    if attempts:
        pytest.fail("Unit test attempted real network, including in a background worker", pytrace=False)


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


@pytest.fixture(autouse=True)
def _isolate_stock_db_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """避免 .env 的 STOCK_DB_PATH 讓測試誤開正式庫。"""
    monkeypatch.setenv("STOCK_DB_PATH", "")
    monkeypatch.setattr("bot.stock_db._configured_stock_db_path", lambda: None)

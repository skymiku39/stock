"""事件鏈 handler 測試。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bot.events import QuantDataFetchCompleted, create_event_bus, reset_event_bus
from bot.events.wiring import wire_chain_handlers


@pytest.fixture(autouse=True)
def _reset_bus() -> None:
    reset_event_bus()
    yield
    reset_event_bus()


class TestEventChain:
    def test_smile_screen_chain_when_enabled(self) -> None:
        bus = create_event_bus(enable_logging=False)
        wire_chain_handlers(bus, enable_smile_screen=True)

        with patch("bot.smile_curve_screener.screen_smile_candidates") as mock_screen:
            mock_screen.return_value = MagicMock(
                candidate_count=2,
                selected_symbols=("2303",),
            )
            bus.publish(QuantDataFetchCompleted(
                symbols=("2303", "2344"),
                start="2023-01-01",
                end="2026-06-12",
            ))

        mock_screen.assert_called_once()
        assert mock_screen.call_args.kwargs["extra_symbols"] == ("2303", "2344")

    def test_chain_disabled_by_default(self) -> None:
        bus = create_event_bus(enable_logging=False)
        wire_chain_handlers(bus, enable_smile_screen=False)

        with patch("bot.smile_curve_screener.screen_smile_candidates") as mock_screen:
            bus.publish(QuantDataFetchCompleted(
                symbols=("2303",),
                start="2023-01-01",
                end="2026-06-12",
            ))

        mock_screen.assert_not_called()

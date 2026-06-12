"""當沖封存閘門測試。"""

from __future__ import annotations

import pytest

from bot.archive_status import day_trading_trade_blocked, trade_block_message
from bot.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(symbols=["2330"], _env_file=None)
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


class TestDayTradingArchive:
    def test_trade_blocked_when_archived(self) -> None:
        s = _settings(
            run_mode="trade",
            day_trading_archived=True,
            day_trading_unfreeze=False,
        )
        assert day_trading_trade_blocked(s) is True

    def test_trade_allowed_when_unfrozen(self) -> None:
        s = _settings(
            run_mode="trade",
            day_trading_archived=True,
            day_trading_unfreeze=True,
        )
        assert day_trading_trade_blocked(s) is False

    def test_watch_not_blocked(self) -> None:
        s = _settings(run_mode="watch")
        assert day_trading_trade_blocked(s) is False

    def test_block_message_mentions_unfreeze(self) -> None:
        msg = trade_block_message(_settings(run_mode="trade"))
        assert "DAY_TRADING_UNFREEZE" in msg

    def test_preflight_archive_fail(self) -> None:
        from bot.preflight import _check_env

        results = _check_env(
            _settings(
                run_mode="trade",
                api_key="k",
                secret_key="s",
                symbols=["2330"],
            ),
        )
        archive = [c for c in results if c.name == "當沖模組封存"]
        assert archive and archive[0].status == "fail"

"""SjBroker safety checks for quote-only modes."""

from __future__ import annotations

from bot.broker import SjBroker
from bot.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(
        symbols=["2330"],
        api_key="key",
        secret_key="secret",
        ca_path="C:/certs/Sinopac.pfx",
        ca_password="pw",
        person_id="TEST_PERSON_ID",
        simulation=False,
        _env_file=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


def test_trade_real_mode_can_activate_ca() -> None:
    broker = SjBroker(_settings(run_mode="trade"))
    assert broker._should_activate_ca() is True


def test_watch_mode_never_activates_ca() -> None:
    broker = SjBroker(_settings(run_mode="watch"))
    assert broker._should_activate_ca() is False


def test_simulation_trade_does_not_activate_ca() -> None:
    broker = SjBroker(_settings(run_mode="trade", simulation=True))
    assert broker._should_activate_ca() is False

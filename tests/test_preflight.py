"""preflight CA 啟用條件與訊息測試。"""

from __future__ import annotations

from bot.config import Settings
from bot.preflight import _ca_skip_reason, _check_ca, _should_test_ca_activation


def _settings(**overrides) -> Settings:
    defaults = dict(
        run_mode="watch",
        simulation=False,
        ca_path="C:/certs/Sinopac.pfx",
        ca_password="pw",
        person_id="A123456789",
        _env_file=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


def test_ca_skip_reason_watch_mode() -> None:
    reason = _ca_skip_reason(_settings(run_mode="watch"))
    assert "RUN_MODE=watch" in reason
    assert "simulation" not in reason.lower()


def test_ca_skip_reason_simulation() -> None:
    reason = _ca_skip_reason(_settings(run_mode="trade", simulation=True))
    assert "SIMULATION=true" in reason


def test_check_ca_info_message_uses_actual_skip_reason() -> None:
    items = _check_ca(_settings(run_mode="watch"))
    info = [c for c in items if c.name == "電子憑證啟用條件"]
    assert len(info) == 1
    assert "RUN_MODE=watch" in info[0].detail
    assert "simulation=true" not in info[0].detail.lower()


def test_should_test_ca_activation_trade_real() -> None:
    assert _should_test_ca_activation(_settings(run_mode="trade")) is True


def test_should_test_ca_activation_watch_without_flag() -> None:
    assert _should_test_ca_activation(_settings(run_mode="watch")) is False


def test_should_test_ca_activation_watch_with_flag() -> None:
    assert _should_test_ca_activation(
        _settings(run_mode="watch"), test_ca_activate=True,
    ) is True

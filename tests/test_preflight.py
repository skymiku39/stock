"""preflight CA 啟用條件與訊息測試。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import bot.preflight as preflight_module
from bot.config import Settings
from bot.preflight import (
    _ca_skip_reason,
    _check_ca,
    _check_account_balance,
    _should_test_ca_activation,
)


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


def test_simulation_balance_is_unknown_not_zero() -> None:
    api = MagicMock()
    api.account_balance.side_effect = AssertionError(
        "simulation balance API should not be called"
    )
    result = _check_account_balance(_settings(simulation=True), api)
    assert result.status == "info"
    assert result.extra == {"balance_available": False, "balance": None}
    assert "不把 0" in result.detail


def test_real_balance_zero_is_a_valid_value() -> None:
    api = SimpleNamespace(account_balance=lambda: SimpleNamespace(acc_balance=0))
    result = _check_account_balance(_settings(simulation=False), api)
    assert result.status == "ok"
    assert result.extra == {"balance_available": True, "balance": 0.0}


def test_run_preflight_loads_settings_from_project_root(
    monkeypatch, tmp_path,
) -> None:
    seen = {}
    fake_settings = SimpleNamespace(
        symbols_auto_merge=False,
        run_mode="watch",
        simulation=True,
        api_key="",
    )

    def _settings(*, _env_file):
        seen["env_file"] = _env_file
        return fake_settings

    monkeypatch.setattr(preflight_module, "Settings", _settings)
    monkeypatch.setattr(preflight_module, "_check_env", lambda *a, **k: [])
    monkeypatch.setattr(preflight_module, "_check_ca", lambda *a, **k: [])
    monkeypatch.setattr(preflight_module, "_check_login", lambda *a, **k: ([], None, None))
    monkeypatch.setattr(preflight_module, "_check_account", lambda *a, **k: [])
    monkeypatch.setattr(preflight_module, "_check_risk", lambda *a, **k: [])
    monkeypatch.setattr(preflight_module, "_check_time", lambda *a, **k: [])
    monkeypatch.setattr(preflight_module, "_check_position_safety", lambda *a, **k: [])
    monkeypatch.setattr(preflight_module, "day_trading_trade_blocked", lambda *a, **k: False)

    report = preflight_module.run_preflight(
        project_root=tmp_path,
        do_real_login=False,
        logger=MagicMock(),
    )

    assert report.can_simulate is True
    assert seen["env_file"] == str(tmp_path.resolve() / ".env")

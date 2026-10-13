from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bot.cloud_sync import TableSyncResult
from bot.cloud_sync_cli import _parse_tables, main


def test_parse_tables_filters_unknown() -> None:
    tables = _parse_tables("stock_info,watchlist")
    assert tables == ["stock_info", "watchlist"]

    with pytest.raises(ValueError, match="未知 table"):
        _parse_tables("not_a_table")


def test_main_disabled_config_returns_2(monkeypatch) -> None:
    monkeypatch.setattr(
        "bot.cloud_sync_cli.load_config_from_env",
        lambda *_args, **_kwargs: MagicMock(enabled=False),
    )
    assert main([]) == 2


def test_main_push_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "bot.cloud_sync_cli.load_config_from_env",
        lambda *_args, **_kwargs: MagicMock(enabled=True),
    )
    fake_sync = MagicMock()
    fake_sync.push_all.return_value = [
        TableSyncResult(table="llm_daily_reports", direction="push", rows=1),
    ]
    with patch("bot.cloud_sync_cli.GoogleSheetSync", return_value=fake_sync):
        with patch("bot.cloud_sync_cli.StockDB") as mock_db:
            mock_db.open.return_value = MagicMock()
            assert main(["--push", "--tables", "llm_daily_reports"]) == 0
            mock_db.open.return_value.close.assert_called_once_with()
    fake_sync.push_all.assert_called_once_with(["llm_daily_reports"])


def test_main_pull_backs_up_first(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "bot.cloud_sync_cli.load_config_from_env",
        lambda *_args, **_kwargs: MagicMock(enabled=True),
    )
    monkeypatch.setattr(
        "bot.cloud_sync_cli.default_db_path",
        lambda *_args, **_kwargs: tmp_path / "stock.db",
    )
    bak_calls: list = []

    def _fake_backup(**kwargs):
        bak_calls.append(kwargs)
        return tmp_path / "backups" / "stock_pre.db"

    monkeypatch.setattr("bot.cloud_sync_cli.backup_stock_db", _fake_backup)
    fake_sync = MagicMock()
    fake_sync.pull_all.return_value = [
        TableSyncResult(table="watchlist", direction="pull", rows=0),
    ]
    with patch("bot.cloud_sync_cli.GoogleSheetSync", return_value=fake_sync):
        with patch("bot.cloud_sync_cli.StockDB") as mock_db:
            mock_db.open.return_value = MagicMock()
            assert main(["--pull", "--tables", "watchlist"]) == 0
    assert len(bak_calls) == 1
    fake_sync.pull_all.assert_called_once_with(["watchlist"])


def test_main_uses_explicit_root_for_config_and_database(monkeypatch, tmp_path) -> None:
    seen: dict[str, object] = {}

    def _config(*args, **kwargs):
        seen["config_root"] = args[0] if args else kwargs.get("root")
        return MagicMock(enabled=True)

    def _db_path(root):
        seen["db_root"] = root
        return tmp_path / "stock.db"

    monkeypatch.setattr("bot.cloud_sync_cli.load_config_from_env", _config)
    monkeypatch.setattr("bot.cloud_sync_cli.default_db_path", _db_path)
    fake_sync = MagicMock()
    fake_sync.push_all.return_value = [
        TableSyncResult(table="watchlist", direction="push", rows=0),
    ]
    with patch("bot.cloud_sync_cli.GoogleSheetSync", return_value=fake_sync):
        with patch("bot.cloud_sync_cli.StockDB") as mock_db:
            mock_db.open.return_value = MagicMock()
            assert main(["--push", "--tables", "watchlist", "--root", str(tmp_path)]) == 0

    assert seen["config_root"] == tmp_path.resolve()
    assert seen["db_root"] == tmp_path.resolve()

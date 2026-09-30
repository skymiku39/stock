from __future__ import annotations

import json

from bot.stock_db import StockDB
from bot.sync_conflict_cli import main


def test_sync_conflict_cli_lists_and_resolves(tmp_path, capsys) -> None:
    db = StockDB.open(root=tmp_path)
    conflict_id = db.record_sync_conflict(
        table_name="watchlist",
        row_key=("2330",),
        local_row={"symbol": "2330", "name": "local"},
        remote_row={"symbol": "2330", "name": "remote"},
    )
    db.close()

    assert main(["--root", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["conflict_id"] == conflict_id
    assert listed[0]["status"] == "open"

    assert main([
        "--root", str(tmp_path),
        "--resolve", conflict_id,
        "--resolution", "保留 local，remote 另存",
        "--json",
    ]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["status"] == "resolved"


def test_sync_conflict_cli_resolves_relative_db_from_root(tmp_path, capsys) -> None:
    db = StockDB.open(path=tmp_path / "custom.db")
    db.record_sync_conflict(
        table_name="watchlist",
        row_key=("2303",),
        local_row={"symbol": "2303"},
        remote_row={"symbol": "2303", "name": "remote"},
    )
    db.close()

    assert main([
        "--root", str(tmp_path),
        "--db", "custom.db",
        "--json",
    ]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["table_name"] == "watchlist"

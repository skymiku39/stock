"""DB 路徑解析與備份測試。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.db_backup_cli import main as backup_main
from bot.stock_db import (
    StockDB,
    StockInfo,
    backup_stock_db,
    default_db_path,
    reset_db_singleton,
)
import bot.stock_db as stock_db_module


_ORIGINAL_CONFIGURED_DB_PATH = stock_db_module._configured_stock_db_path


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    reset_db_singleton()
    yield
    reset_db_singleton()


def test_default_db_path_uses_root_when_no_override(tmp_path: Path) -> None:
    assert default_db_path(tmp_path) == tmp_path / "data" / "stock.db"


def test_default_db_path_honors_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = tmp_path / "shared" / "custom.db"
    monkeypatch.setenv("STOCK_DB_PATH", str(custom))
    monkeypatch.setattr(
        "bot.stock_db._configured_stock_db_path",
        lambda: Path(str(custom)),
    )
    assert default_db_path(tmp_path) == custom


def test_default_db_path_reads_explicit_root_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI --root 必須使用目標專案的 STOCK_DB_PATH，而非目前 cwd。"""
    monkeypatch.delenv("STOCK_DB_PATH", raising=False)
    monkeypatch.setattr(
        stock_db_module,
        "_configured_stock_db_path",
        _ORIGINAL_CONFIGURED_DB_PATH,
    )
    (tmp_path / ".env").write_text(
        "STOCK_DB_PATH=data/project-specific.db\n",
        encoding="utf-8",
    )
    assert default_db_path(tmp_path) == tmp_path / "data" / "project-specific.db"


def test_backup_stock_db_copies_data(tmp_path: Path) -> None:
    src = tmp_path / "stock.db"
    db = StockDB.open(path=src)
    db.upsert_stock_info(StockInfo(symbol="2330", name="台積電"))
    db.close()

    dest = backup_stock_db(src=src, dest_dir=tmp_path / "backups", keep=5)
    assert dest.is_file()
    assert dest.parent == tmp_path / "backups"
    assert not list(dest.parent.glob(".*.tmp"))

    restored = StockDB.open(path=dest)
    info = restored.get_stock_info("2330")
    assert info is not None and info.name == "台積電"
    restored.close()


def test_backup_stock_db_prunes_old(tmp_path: Path) -> None:
    src = tmp_path / "stock.db"
    StockDB.open(path=src).close()
    dest_dir = tmp_path / "backups"
    for i in range(4):
        backup_stock_db(src=src, dest_dir=dest_dir, keep=2, label=f"n{i}")
    left = list(dest_dir.glob("stock_*.db"))
    assert len(left) == 2


def test_backup_cli_json_success(tmp_path: Path, capsys) -> None:
    import json

    src = tmp_path / "stock.db"
    StockDB.open(path=src).close()
    code = backup_main([
        "--src", str(src),
        "--dest-dir", str(tmp_path / "bak"),
        "--json",
    ])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    assert Path(payload["artifacts"]["backup"]).is_file()


def test_backup_cli_root_resolves_default_db_outside_project_cwd(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    StockDB.open(root=project).close()
    monkeypatch.chdir(outside)

    code = backup_main([
        "--root", str(project),
        "--dest-dir", "backups",
        "--json",
    ])
    payload = __import__("json").loads(capsys.readouterr().out)

    assert code == 0
    assert Path(payload["src"]) == project / "data" / "stock.db"
    assert Path(payload["artifacts"]["backup"]).parent == project / "backups"


def test_backup_cli_missing_src_returns_2(tmp_path: Path, capsys) -> None:
    code = backup_main([
        "--src", str(tmp_path / "missing.db"),
        "--json",
    ])
    assert code == 2

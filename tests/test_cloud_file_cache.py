from __future__ import annotations

from pathlib import Path

from bot.cloud_file_cache import (
    cloud_path_for,
    mirror_file_to_cloud,
    read_json_cache,
    restore_file_from_cloud,
    restore_tree_from_cloud,
    write_json_cache,
)


def test_disabled_without_google_cache_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("GOOGLE_CACHE_DIR", raising=False)
    local = tmp_path / "project" / "data" / "x.json"
    local.parent.mkdir(parents=True)
    local.write_text("{}", encoding="utf-8")

    assert cloud_path_for(local, root=tmp_path / "project") is None
    assert mirror_file_to_cloud(local, root=tmp_path / "project") is False


def test_mirror_and_restore_file(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    cloud = tmp_path / "drive-cache"
    monkeypatch.setenv("GOOGLE_CACHE_DIR", str(cloud))

    local = project / "data" / "chips" / "2026-05-28" / "qfii_daily.json"
    local.parent.mkdir(parents=True)
    local.write_text('{"ok": true}', encoding="utf-8")

    assert mirror_file_to_cloud(local, root=project) is True
    mirrored = cloud / "data" / "chips" / "2026-05-28" / "qfii_daily.json"
    assert mirrored.read_text(encoding="utf-8") == '{"ok": true}'

    local.unlink()
    assert restore_file_from_cloud(local, root=project) is True
    assert local.read_text(encoding="utf-8") == '{"ok": true}'


def test_json_helpers_round_trip_through_cloud(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    cloud = tmp_path / "drive-cache"
    monkeypatch.setenv("GOOGLE_CACHE_DIR", str(cloud))

    local = project / "data" / "macro" / "macro_2026-05-28.json"
    write_json_cache(local, {"asof": "2026-05-28"}, root=project, indent=2)
    local.unlink()

    assert read_json_cache(local, root=project) == {"asof": "2026-05-28"}


def test_restore_tree_from_cloud(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    cloud = tmp_path / "drive-cache"
    monkeypatch.setenv("GOOGLE_CACHE_DIR", str(cloud))

    cloud_file = cloud / "data" / "etf_holdings" / "00980A" / "2026-05-28.csv"
    cloud_file.parent.mkdir(parents=True)
    cloud_file.write_text("ticker,name\n2330,TSMC\n", encoding="utf-8")

    copied = restore_tree_from_cloud(
        project / "data" / "etf_holdings" / "00980A",
        root=project,
    )

    assert copied == 1
    restored = project / "data" / "etf_holdings" / "00980A" / "2026-05-28.csv"
    assert restored.read_text(encoding="utf-8") == "ticker,name\n2330,TSMC\n"

from __future__ import annotations


def test_validate_cli_passes_explicit_root(monkeypatch, tmp_path, capsys) -> None:
    from bot import validate_sources

    seen: dict[str, object] = {}

    def fake_run_all(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(validate_sources, "run_all", fake_run_all)
    assert validate_sources.main(["--root", str(tmp_path)]) == 0
    assert seen["root"] == tmp_path
    assert "總計 0" in capsys.readouterr().out


def test_validate_cli_resolves_relative_reports_under_root(
    monkeypatch, tmp_path, capsys,
) -> None:
    from bot import validate_sources

    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setattr(validate_sources, "run_all", lambda **kwargs: [])

    assert validate_sources.main([
        "--root", str(project),
        "--json", "data/validation/report.json",
        "--md", "data/validation/report.md",
    ]) == 0
    capsys.readouterr()

    assert (project / "data/validation/report.json").is_file()
    assert (project / "data/validation/report.md").is_file()
    assert not (outside / "data/validation/report.json").exists()

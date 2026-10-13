"""stock-test CLI：subprocess 路由、cwd、exit code 與參數契約。"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import test_cli
from bot.test_cli import UNIT_MARKER_EXPR, build_pytest_argv, main


def test_build_unit_argv_includes_marker_and_tests_path(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    argv = build_pytest_argv("unit", tmp_path, ["-q"])
    assert argv[:3] == ["-m", "pytest", str(tmp_path / "tests")]
    assert argv[3:5] == ["-m", UNIT_MARKER_EXPR]
    assert argv[-1] == "-q"


def test_build_regression_argv_has_no_marker_filter(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    argv = build_pytest_argv("regression", tmp_path, ["-k", "foo"])
    assert argv == ["-m", "pytest", str(tmp_path / "tests"), "-k", "foo"]


@pytest.mark.parametrize(
    "override_args",
    [
        ["-m", "integration"],
        ["-m", "slow"],
        ["-mslow"],
        ["-mintegration"],
        ["--markexpr", "slow"],
        ["--markexpr=integration"],
        ["-q", "-mslow"],
        ["-mslow", "-q"],
    ],
)
def test_unit_rejects_marker_override_variants(
    tmp_path: Path, override_args: list[str]
) -> None:
    (tmp_path / "tests").mkdir()
    with pytest.raises(SystemExit) as exc_info:
        build_pytest_argv("unit", tmp_path, override_args)
    assert exc_info.value.code == 2


def test_unit_subprocess_routing_cwd_and_returncode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "tests").mkdir()
    seen: dict[str, object] = {}

    def fake_run(cmd, cwd=None, **_kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(test_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(test_cli, "_pytest_installed", lambda: True)

    code = main(["--root", str(tmp_path), "unit", "--", "-q", "-k", "expr"])
    assert code == 7
    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0] == sys.executable
    assert cmd[1:4] == ["-m", "pytest", str(tmp_path / "tests")]
    assert cmd[4:6] == ["-m", UNIT_MARKER_EXPR]
    assert cmd[6:] == ["-q", "-k", "expr"]
    assert seen["cwd"] == tmp_path.resolve()


def test_regression_subprocess_preserves_passthrough_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "tests").mkdir()
    seen: dict[str, object] = {}

    def fake_run(cmd, cwd=None, **_kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(test_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(test_cli, "_pytest_installed", lambda: True)

    assert main(["--root", str(tmp_path), "regression", "--", "-vv", "-x"]) == 0
    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    assert "-m" not in cmd[3:]  # no profile marker after pytest module args
    assert cmd[-2:] == ["-vv", "-x"]
    assert seen["cwd"] == tmp_path.resolve()


def test_invalid_root_missing_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(test_cli, "_pytest_installed", lambda: True)
    with pytest.raises(SystemExit) as exc_info:
        main(["--root", str(tmp_path), "unit"])
    assert exc_info.value.code == 2
    assert "missing tests/" in capsys.readouterr().err


def test_invalid_root_nonexistent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope"
    monkeypatch.setattr(test_cli, "_pytest_installed", lambda: True)
    with pytest.raises(SystemExit) as exc_info:
        main(["--root", str(missing), "regression"])
    assert exc_info.value.code == 2
    assert "does not exist" in capsys.readouterr().err


def test_missing_pytest_returns_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(test_cli, "_pytest_installed", lambda: False)
    code = main(["--root", str(tmp_path), "unit"])
    assert code == 1
    err = capsys.readouterr().err
    assert "pytest is not installed" in err
    assert "uv sync --extra dev" in err


def test_help_works_without_pytest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(test_cli, "_pytest_installed", lambda: False)
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "stock-test" in out
    assert "unit" in out
    assert "regression" in out


def test_module_execution_help() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "bot.test_cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0
    assert "stock-test" in completed.stdout
    assert "unit" in completed.stdout


def test_console_entry_help() -> None:
    completed = subprocess.run(
        ["uv", "run", "--no-sync", "stock-test", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0
    assert "stock-test" in completed.stdout
    assert "unit" in completed.stdout
    assert "regression" in completed.stdout


def _write_tiny_suite(root: Path) -> None:
    """Create a minimal pytest project with unmarked / integration / slow cases."""
    tests = root / "tests"
    tests.mkdir()
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [tool.pytest.ini_options]
            testpaths = ["tests"]
            markers = [
                "integration: integration tests",
                "slow: slow tests",
            ]
            """
        ),
        encoding="utf-8",
    )
    (tests / "test_tiny_suite.py").write_text(
        textwrap.dedent(
            """\
            import pytest

            def test_unmarked_ok():
                assert True

            @pytest.mark.integration
            def test_integration_case():
                assert True

            @pytest.mark.slow
            def test_slow_case():
                assert True
            """
        ),
        encoding="utf-8",
    )


def test_unit_excludes_marked_via_real_tiny_suite(tmp_path: Path) -> None:
    _write_tiny_suite(tmp_path)
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "stock-test",
            "--root",
            str(tmp_path),
            "unit",
            "--",
            "--collect-only",
            "-q",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        cwd=Path.cwd(),
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    out = completed.stdout + completed.stderr
    assert "test_unmarked_ok" in out
    assert "test_integration_case" not in out
    assert "test_slow_case" not in out


def test_regression_includes_marked_via_real_tiny_suite(tmp_path: Path) -> None:
    _write_tiny_suite(tmp_path)
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "stock-test",
            "--root",
            str(tmp_path),
            "regression",
            "--",
            "--collect-only",
            "-q",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        cwd=Path.cwd(),
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    out = completed.stdout + completed.stderr
    assert "test_unmarked_ok" in out
    assert "test_integration_case" in out
    assert "test_slow_case" in out

import pytest

from bot import main as bot_main


def test_stock_bot_help_is_safe(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        bot_main.main(["--help"])

    assert exc.value.code == 0
    assert "--root" in capsys.readouterr().out


def test_stock_bot_rejects_missing_root() -> None:
    with pytest.raises(SystemExit) as exc:
        bot_main.main(["--root", "does-not-exist-hopefully"])

    assert exc.value.code == 2

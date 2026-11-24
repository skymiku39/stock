"""python -m bot.notifier --test 的測試：不連網、不印 token。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot import config, notifier


def _settings(token, chat):
    return lambda: SimpleNamespace(telegram_bot_token=token, telegram_chat_id=chat)


def test_cli_not_configured(monkeypatch, capsys):
    monkeypatch.setattr(config, "Settings", _settings("", ""))
    monkeypatch.setattr(notifier.requests, "post", lambda *a, **k: pytest.fail("不該連網"))
    assert notifier.main(["--test", "hi"]) == 1
    assert "未設定" in capsys.readouterr().out


def test_cli_sent_and_no_token_printed(monkeypatch, capsys):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        return SimpleNamespace(ok=True, text="")

    monkeypatch.setattr(config, "Settings", _settings("SECRET-TOKEN", "42"))
    monkeypatch.setattr(notifier.requests, "post", fake_post)
    assert notifier.main(["--test", "你好 <&>"]) == 0
    out = capsys.readouterr()
    assert "已送出" in out.out and "SECRET-TOKEN" not in out.out + out.err
    assert calls == [{"chat_id": "42", "text": "你好 <&>"}]


def test_cli_failure(monkeypatch, capsys):
    monkeypatch.setattr(config, "Settings", _settings("T", "1"))
    monkeypatch.setattr(notifier.requests, "post", lambda *a, **k: SimpleNamespace(ok=False, text="bad"))
    assert notifier.main(["--test", "hi"]) == 1
    assert "失敗" in capsys.readouterr().out


def test_send_plain_exception_returns_false(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("https://api.telegram.org/botSECRET/sendMessage")

    monkeypatch.setattr(notifier.requests, "post", boom)
    assert notifier.TelegramNotifier("SECRET", "1").send_plain("x") is False

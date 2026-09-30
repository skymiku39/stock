from __future__ import annotations

from types import SimpleNamespace


def test_ask_cli_passes_explicit_root_to_engine(monkeypatch, tmp_path, capsys) -> None:
    from bot import ask_cli

    seen: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, **kwargs):
            seen["root"] = kwargs["root"]

        def ask(self, query, **kwargs):
            return SimpleNamespace(
                answer="ok",
                status="ok",
                trace_id="trace-test",
                evidence=SimpleNamespace(usable_items=[]),
                plan=SimpleNamespace(plan_id="plan-test"),
                verification=SimpleNamespace(errors=[]),
            )

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(ask_cli, "AskEngine", FakeEngine)
    assert ask_cli.main(["--root", str(tmp_path), "--no-llm", "測試問題"]) == 0
    assert seen["root"] == tmp_path.resolve()
    assert seen["closed"] is True
    assert "status=ok" in capsys.readouterr().out

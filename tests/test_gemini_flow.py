from __future__ import annotations

from bot import gemini_smoke
from bot.llm_analyzer import DEFAULT_MODEL, GeminiClient, gemini_call
from bot.llm_log import LlmCallLogger
from bot.prompt_registry import PromptRegistry


def test_gemini_client_disabled_without_api_key() -> None:
    client = GeminiClient(api_key="", model=DEFAULT_MODEL)

    assert client.enabled is False


def test_gemini_call_disabled_records_audit_log(tmp_path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "smoke.yaml").write_text(
        "\n".join(
            [
                "id: smoke",
                'version: "1.0"',
                "description: Smoke prompt",
                "max_output_tokens: 64",
                "temperature: 0.0",
                "inputs:",
                "  - name: name",
                "    required: true",
                "template: |",
                "  Hello {name}",
            ]
        ),
        encoding="utf-8",
    )

    registry = PromptRegistry(prompts_dir=prompts_dir)
    call_logger = LlmCallLogger(log_dir=tmp_path / "llm_calls")
    client = GeminiClient(api_key="", model=DEFAULT_MODEL)

    raw, info = gemini_call(
        "smoke",
        client=client,
        registry=registry,
        call_logger=call_logger,
        metadata={"test": "disabled-flow"},
        name="Gemini",
    )

    assert raw is None
    assert info["disabled"] is True

    records = call_logger.read()
    assert len(records) == 1
    record = records[0]
    assert record.prompt_id == "smoke"
    assert record.prompt_version == "1.0"
    assert record.input == "Hello Gemini"
    assert record.output == ""
    assert record.success is False
    assert record.error == "LLM disabled (no API key or SDK)"
    assert record.metadata == {"test": "disabled-flow"}


def test_gemini_smoke_cli_requires_key(monkeypatch, capsys) -> None:
    """gemini_smoke --provider gemini 在無 API Key 時應回傳非零 exit code。"""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GEMINI_GATEWAY_BASE_URL", "")
    monkeypatch.setenv("CURSOR_LLM_BASE_URL", "")

    exit_code = gemini_smoke.main([])

    assert exit_code != 0

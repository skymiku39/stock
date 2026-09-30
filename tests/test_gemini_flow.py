from __future__ import annotations

import json
from types import SimpleNamespace

from bot import gemini_smoke
from bot.llm_analyzer import DEFAULT_MODEL, GeminiClient, gemini_call
from bot import llm_log
from bot.llm_log import LlmCallLogger
from bot.prompt_registry import PromptRegistry
from bot.stock_db import StockDB, reset_db_singleton


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
    assert info["analysis_id"].endswith("_smoke_NA")

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


def test_gemini_call_loads_prompts_from_explicit_project_root(tmp_path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "root_only.yaml").write_text(
        "\n".join([
            "id: root_only",
            'version: "1.0"',
            "inputs:",
            "  - name: value",
            "    required: true",
            "template: |",
            "  Root prompt {value}",
        ]),
        encoding="utf-8",
    )

    logger = LlmCallLogger(log_dir=tmp_path / "llm_calls")
    raw, info = gemini_call(
        "root_only",
        client=GeminiClient(api_key="", model=DEFAULT_MODEL),
        call_logger=logger,
        root=tmp_path,
        value="OK",
    )

    assert raw is None
    assert info["disabled"] is True
    assert logger.read()[0].input == "Root prompt OK"


def test_custom_log_dir_without_root_does_not_mirror_formal_db(tmp_path, monkeypatch) -> None:
    calls = []

    def fail_if_mirrored(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(llm_log, "_mirror_to_stock_db", fail_if_mirrored)
    logger = LlmCallLogger(log_dir=tmp_path / "isolated-llm-calls")

    logger.record(
        prompt_id="isolated",
        prompt_version="1.0",
        model="test",
        input_text="input",
        output_text="output",
    )

    assert logger.root is None
    assert calls == []


def test_llm_mirror_keeps_evidence_ids_and_trace(tmp_path) -> None:
    logger = LlmCallLogger(root=tmp_path)
    logger.record(
        prompt_id="evidence-test",
        prompt_version="1.0",
        model="test",
        input_text="input",
        output_text="output",
        metadata={
            "evidence_ids": ["e-1", "e-1", "e-2"],
            "trace_id": "trace-evidence",
        },
    )

    row = StockDB.open(root=tmp_path).list_llm_analysis(limit=1)[0]
    assert json.loads(row.evidence_json) == ["e-1", "e-2"]
    assert row.source_record_id == "trace-evidence"


def test_gemini_call_infers_ticker_and_mirrors_to_root_db(tmp_path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "ticker.yaml").write_text(
        "\n".join([
            "id: ticker",
            'version: "1.0"',
            "description: Ticker prompt",
            "max_output_tokens: 64",
            "temperature: 0.0",
            "inputs:",
            "  - name: ticker",
            "    required: true",
            "template: |",
            "  Review {ticker}",
        ]),
        encoding="utf-8",
    )

    registry = PromptRegistry(prompts_dir=prompts_dir)
    logger = LlmCallLogger(root=tmp_path)
    try:
        raw, _info = gemini_call(
            "ticker",
            client=GeminiClient(api_key="", model=DEFAULT_MODEL),
            registry=registry,
            call_logger=logger,
            root=tmp_path,
            metadata={"task": "ticker-test"},
            ticker="2330",
        )

        assert raw is None
        record = logger.read()[0]
        assert record.metadata["ticker"] == "2330"
        row = StockDB.open(root=tmp_path).list_llm_analysis(ticker="2330", limit=1)
        assert len(row) == 1
        assert row[0].ticker == "2330"
    finally:
        reset_db_singleton()


def test_gemini_smoke_cli_requires_key(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        gemini_smoke,
        "Settings",
        lambda *_args, **_kwargs: SimpleNamespace(gemini_api_key="", gemini_model=DEFAULT_MODEL),
    )

    exit_code = gemini_smoke.main([])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "GEMINI_API_KEY is not set" in captured.out

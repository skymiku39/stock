"""LlmGate 單元測試。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.config import Settings
from bot.llm_gate import LlmGate
from bot.models import PositionInfo
from bot.risk_guard import RiskGuard


def _settings(**kw) -> Settings:
    defaults = dict(symbols=["2330"], _env_file=None)
    defaults.update(kw)
    return Settings(**defaults)  # type: ignore[call-arg]


def _write_llm_cache(tmp_path: Path, symbol: str, payload: dict) -> None:
    d = tmp_path / "data" / "auto_llm"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{symbol}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )


class TestLlmGateEntry:
    def test_disabled_allows(self, tmp_path: Path) -> None:
        gate = LlmGate(_settings(llm_gate_enabled=False), project_root=tmp_path)
        v = gate.allow_entry("2330", 100.0, 2.0)
        assert v.allowed is True

    def test_no_cache_blocks(self, tmp_path: Path) -> None:
        settings = _settings(llm_gate_enabled=True)
        risk = RiskGuard(settings, project_root=tmp_path)
        gate = LlmGate(settings, risk=risk, project_root=tmp_path)
        v = gate.allow_entry("2330", 100.0, 2.0)
        assert v.allowed is False
        assert v.reason == "no_llm_cache"

    def test_positive_cache_allows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_llm_cache(tmp_path, "2330", {
            "sentiment": "positive",
            "sentiment_score": 0.5,
            "confidence": 0.8,
            "fetched_at": "2026-06-05T08:00:00",
        })
        monkeypatch.setattr(
            "bot.llm_gate.compute_scorecard",
            lambda **kw: type("C", (), {"timeframes": {"day_trade": type("T", (), {"total": 70.0})()}})(),
        )
        gate = LlmGate(
            _settings(llm_gate_enabled=True, llm_min_day_trade_score=62),
            project_root=tmp_path,
        )
        v = gate.allow_entry("2330", 100.0, 2.0)
        assert v.allowed is True

    def test_negative_sentiment_blocks(self, tmp_path: Path) -> None:
        _write_llm_cache(tmp_path, "2330", {
            "sentiment": "negative",
            "sentiment_score": -0.3,
            "confidence": 0.9,
        })
        gate = LlmGate(_settings(llm_gate_enabled=True), project_root=tmp_path)
        v = gate.allow_entry("2330", 100.0, 2.0)
        assert v.allowed is False
        assert v.reason == "negative_sentiment"


class TestLlmGateExit:
    def test_exit_on_negative(self, tmp_path: Path) -> None:
        _write_llm_cache(tmp_path, "2330", {
            "sentiment": "negative",
            "confidence": 0.6,
        })
        gate = LlmGate(
            _settings(llm_gate_enabled=True, llm_exit_on_negative=True),
            project_root=tmp_path,
        )
        pos = PositionInfo(symbol="2330", avg_price=100.0, quantity=10, unit="share")
        assert gate.should_exit("2330", pos) == "llm_neg"

    def test_exit_disabled(self, tmp_path: Path) -> None:
        _write_llm_cache(tmp_path, "2330", {
            "sentiment": "negative",
            "confidence": 0.9,
        })
        gate = LlmGate(_settings(llm_gate_enabled=True), project_root=tmp_path)
        pos = PositionInfo(symbol="2330", avg_price=100.0, quantity=1)
        assert gate.should_exit("2330", pos) is None

"""Source grounding must not infer facts from shared words or digits."""
from __future__ import annotations

import pytest

from bot.answer_verifier import verify_answer_payload
from bot.evidence import EvidencePack, make_evidence


def verify(excerpt: str, claim: str, answer: str | None = None):
    item = make_evidence("fundamental", source="twse", excerpt=excerpt)
    pack = EvidencePack(query="營收？", plan_id="p", trace_id="t", items=[item])
    return verify_answer_payload({
        "answer": answer if answer is not None else claim,
        "confidence": 0.8,
        "evidence_ids": [item["id"]],
        "claims": [{"text": claim, "evidence_ids": [item["id"]]}],
    }, pack)


@pytest.mark.parametrize(("excerpt", "claim"), [
    ("營收上升 10%", "營收下降 10%"),
    ("營收上升 110%", "營收上升 10%"),
    ("營收 100 元", "營收 10 元"),
    ("營收上升 10%", "10"),
    ("營收上升 110%", "10%"),
    ("2330 營收 10 元", "2317 營收 10 元"),
    ("2025 年營收 10 元", "2026 年營收 10 元"),
    ("營收 10 億元", "營收 10 元"),
    ("營收 10 元；毛利 5 元", "毛利 10 元"),
    ("並非營收上升 10%", "營收上升 10%"),
    ("預計營收上升 10%", "營收上升 10%"),
])
def test_rejects_changed_fact(excerpt: str, claim: str) -> None:
    assert not verify(excerpt, claim).valid


@pytest.mark.parametrize("answer", [
    "營收下降 10%", "營收上升 10%；股價必然大漲", "營收上升 10%，因此沒有風險",
])
def test_rejects_unsupported_answer_with_correct_claim(answer: str) -> None:
    result = verify("營收上升 10%", "營收上升 10%", answer)
    assert not result.valid
    assert any("正文" in error for error in result.errors)


def test_correct_quoted_fact_retains_attribution_and_entity() -> None:
    result = verify("2330 五月營收年增 10%", "五月營收年增 10%", "根據公告，2330 五月營收年增 10%。")
    assert result.valid, result.errors
    assert result.scope == "source_grounding"


def test_numeric_formatting_and_grouping_are_equivalent() -> None:
    result = verify("營收 1,000.00 元", "營收 1000 元")
    assert result.valid, result.errors


def test_spaced_attribution_and_ascii_fact_are_supported() -> None:
    result = verify("2330 公布重大訊息 fixture", "2330 公布重大訊息 fixture", "根據 MOPS，2330 公布重大訊息 fixture。")
    assert result.valid, result.errors

"""fixture-driven MOPS presentation domain validator tests."""

from __future__ import annotations

from pathlib import Path

from bot.data_quality import QUALITY_INVALID, QUALITY_OK, QUALITY_PARTIAL
from bot.mops_presentation_validator import (
    resolve_conference_date,
    validate_mops_presentation,
    validate_mops_presentation_file,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mops_presentation"


def test_fixture_rejects_html_masquerading_as_pdf() -> None:
    path = FIXTURES / "html_masquerading.pdf"
    result = validate_mops_presentation_file(
        path,
        mime_type="application/pdf",
        claimed_conference_date="2026-09-19",
        source_as_of="2026-09-19",
    )
    assert result.quality_status == QUALITY_INVALID
    assert result.content_kind == "html"
    assert any("HTML" in message for message in result.errors)


def test_fixture_minimal_pdf_magic_mime_suffix() -> None:
    path = FIXTURES / "minimal.pdf"
    result = validate_mops_presentation_file(
        path,
        mime_type="application/pdf",
        claimed_conference_date="2026-09-19",
        source_as_of="2026-09-19",
        calendar_conference_date="2026-09-19",
    )
    assert result.is_pdf
    assert result.content_kind == "pdf"
    assert result.usable
    assert result.conference_date_aligned
    assert result.conference_date == "2026-09-19"
    assert result.quality_status in {QUALITY_OK, QUALITY_PARTIAL}


def test_conference_date_requires_raw_or_calendar_evidence() -> None:
    accepted, aligned, findings = resolve_conference_date(
        "2026-09-19",
        source_as_of="",
        calendar_conference_date="",
    )
    assert accepted == ""
    assert aligned is False
    assert any(item.check == "conference_date" for item in findings)

    accepted, aligned, _ = resolve_conference_date(
        "2026-09-19",
        source_as_of="2026-09-19",
        calendar_conference_date="",
    )
    assert accepted == "2026-09-19"
    assert aligned is True


def test_page_count_exposed_when_provided() -> None:
    payload = (FIXTURES / "minimal.pdf").read_bytes()
    result = validate_mops_presentation(
        content=payload,
        content_path="2330_2026-09-19.pdf",
        mime_type="application/pdf",
        suffix="pdf",
        page_count=27,
        claimed_conference_date="2026-09-19",
        calendar_conference_date="2026-09-19",
    )
    assert result.page_count == 27
    assert result.usable


def test_unknown_source_not_upgraded_to_ok() -> None:
    payload = (FIXTURES / "minimal.pdf").read_bytes()
    result = validate_mops_presentation(
        content=payload,
        content_path="deck.pdf",
        mime_type="application/pdf",
        suffix="pdf",
        claimed_conference_date="2026-09-19",
        source_as_of="2026-09-19",
        source_quality_status="unknown",
    )
    assert result.quality_status == QUALITY_PARTIAL



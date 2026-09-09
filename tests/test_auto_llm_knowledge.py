"""自動研究知識組成：法說原文進 research_ticker、行事曆 URL、PDF 抽字。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot.auto_llm import (
    MIN_PRESENTATION_CHARS,
    _format_calendar_text,
    _gather_pipeline_text,
    _resolve_presentation_block,
    _try_research_ticker,
)
from bot.mops_scraper import PresentationText


def test_format_calendar_text_includes_presentation_url():
    entry = SimpleNamespace(
        date=dt.date(2026, 5, 1),
        time="14:00",
        ticker="2330",
        company="台積電",
        note="線上",
        presentation_url="mops-calendar://110420260501M001.pdf",
    )
    text = _format_calendar_text([entry])
    assert "簡報: mops-calendar://110420260501M001.pdf" in text
    assert "2330" in text


def test_gather_pipeline_text_reads_txt(tmp_path: Path):
    d = tmp_path / "data" / "mops_downloads"
    d.mkdir(parents=True)
    body = "營收指引上調 " + ("內容" * 120)
    (d / "2330_2026-05-01.txt").write_text(body, encoding="utf-8")
    out = _gather_pipeline_text("2330", root=tmp_path)
    assert "營收指引上調" in out
    assert "2330_2026-05-01.txt" in out


def test_gather_pipeline_text_reads_pdf(tmp_path: Path):
    d = tmp_path / "data" / "mops_downloads"
    d.mkdir(parents=True)
    pdf = d / "2330_conf.pdf"
    pdf.write_bytes(b"%PDF-fake")
    fake = PresentationText(
        ticker="2330",
        source=str(pdf),
        text="毛利率展望 " + ("x" * MIN_PRESENTATION_CHARS),
        pages=1,
    )
    with patch("bot.mops_scraper.extract_pdf_text", return_value=fake):
        out = _gather_pipeline_text("2330", root=tmp_path)
    assert "毛利率展望" in out


def test_resolve_presentation_block_downloads_when_local_empty(tmp_path: Path):
    past = [
        SimpleNamespace(
            date=dt.date(2026, 4, 1),
            presentation_url="mops-calendar://demo.pdf",
        ),
    ]
    fake = PresentationText(
        ticker="2330",
        source="demo",
        text="資本支出擴張 " + ("y" * MIN_PRESENTATION_CHARS),
    )
    logger = MagicMock()
    with patch("bot.mops_scraper.download_presentation", return_value=fake):
        out = _resolve_presentation_block(
            "2330",
            root=tmp_path,
            calendar_data={"past": past},
            logger=logger,
            enable_download=True,
        )
    assert "資本支出擴張" in out


def test_resolve_presentation_block_skips_download_when_local_ok(tmp_path: Path):
    d = tmp_path / "data" / "mops_downloads"
    d.mkdir(parents=True)
    body = "本地法說原文 " + ("z" * MIN_PRESENTATION_CHARS)
    (d / "2330_local.txt").write_text(body, encoding="utf-8")
    past = [
        SimpleNamespace(
            date=dt.date(2026, 4, 1),
            presentation_url="mops-calendar://demo.pdf",
        ),
    ]
    logger = MagicMock()
    with patch("bot.mops_scraper.download_presentation") as dl:
        out = _resolve_presentation_block(
            "2330",
            root=tmp_path,
            calendar_data={"past": past},
            logger=logger,
            enable_download=True,
        )
    dl.assert_not_called()
    assert "本地法說原文" in out


def test_try_research_ticker_passes_presentation_block():
    client = MagicMock()
    client.model = "gemini-test"
    logger = MagicMock()
    captured = {}

    def _fake_gemini_call(prompt_id, *, client, metadata=None, **kwargs):
        captured["prompt_id"] = prompt_id
        captured["kwargs"] = kwargs
        return (
            '{"summary":"ok","sentiment":"neutral","sentiment_score":0,'
            '"confidence":0.5,"key_metrics":{},"growth_drivers":[],'
            '"risks":[],"capex_signal":"未提及","margin_outlook":"未提及"}',
            {"prompt_id": "research_ticker", "prompt_version": "1.1"},
        )

    fake_reg = MagicMock()
    fake_reg.get.return_value = object()
    with patch("bot.prompt_registry.get_registry", return_value=fake_reg), patch(
        "bot.auto_llm.gemini_call", side_effect=_fake_gemini_call,
    ):
        res = _try_research_ticker(
            "2330",
            name="台積電",
            upcoming=[],
            past=[],
            materials=[],
            news=[],
            web_block="",
            client=client,
            logger=logger,
            presentation_block="這是法說簡報原文內容足夠長",
        )
    assert res is not None
    assert captured["prompt_id"] == "research_ticker"
    assert captured["kwargs"]["presentation_block"] == "這是法說簡報原文內容足夠長"


def test_research_ticker_prompt_declares_presentation_block():
    from bot.prompt_registry import get_registry

    reg = get_registry()
    p = reg.get("research_ticker")
    assert p is not None
    assert p.version == "1.1"
    names = {i.name for i in p.inputs}
    assert "presentation_block" in names
    rendered = p.render(
        ticker="2330",
        name="台積電",
        today="2026-09-09",
        upcoming_events="(無)",
        past_events="(無)",
        presentation_block="簡報測試原文",
        news_block="(無)",
        web_block="(無)",
    )
    assert "簡報測試原文" in rendered
    assert "法說會簡報" in rendered

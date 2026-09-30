"""自動研究知識組成：法說原文進 research_ticker、行事曆 URL、PDF 抽字。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot.auto_llm import (
    MIN_PRESENTATION_CHARS,
    _format_calendar_text,
    _gather_pipeline_text,
    _load_research_memory_context,
    _resolve_presentation_block,
    _try_research_ticker,
    auto_analyze_ticker,
    _build_source_sections,
    _select_verified_long_memory_evidence,
)
from bot.evidence import make_evidence, persist_evidence_items
from bot.memory_store import MemoryStore
from bot.stock_db import LlmAnalysisRow, StockDB
from bot.mops_scraper import PresentationText


def test_llm_research_batch_loads_settings_from_root(monkeypatch, tmp_path: Path) -> None:
    from bot import auto_llm as module

    seen: dict[str, object] = {}

    def _settings(*args, **kwargs):
        seen["env_file"] = kwargs.get("_env_file")
        return SimpleNamespace(gemini_api_key="")

    monkeypatch.setattr(module, "Settings", _settings)
    assert module.run_llm_research_batch(tickers=[], root=tmp_path) == 2
    assert seen["env_file"] == str(tmp_path.resolve() / ".env")


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
            {"prompt_id": "research_ticker", "prompt_version": "1.2"},
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
            quant_block="【基本面】\n- PER=20",
        )
    assert res is not None
    assert captured["prompt_id"] == "research_ticker"
    assert captured["kwargs"]["presentation_block"] == "這是法說簡報原文內容足夠長"
    assert "PER=20" in captured["kwargs"]["quant_block"]


def test_auto_research_registers_evidence_and_links_llm_row(tmp_path: Path):
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_analysis(LlmAnalysisRow(
        id="analysis-1",
        ts="2026-09-17T10:00:00+08:00",
        ticker="2330",
        prompt_id="research_ticker",
        prompt_version="1.2",
    ))
    evidence = make_evidence(
        "news",
        source="fixture",
        source_ref="news:1",
        title="台積電測試新聞",
        excerpt="2330 營收成長",
    )
    data = {
        "summary": "研究摘要",
        "sentiment": "neutral",
        "sentiment_score": 0.0,
        "confidence": 0.6,
        "key_metrics": {},
        "growth_drivers": [],
        "risks": [],
        "capex_signal": "未提及",
        "margin_outlook": "未提及",
    }
    result = {
        "raw": json.dumps(data, ensure_ascii=False),
        "info": {
            "prompt_id": "research_ticker",
            "prompt_version": "1.2",
            "analysis_id": "analysis-1",
        },
        "data": data,
    }
    client = SimpleNamespace(enabled=True, model="test-model")
    with patch("bot.auto_llm.load_cached_auto_analysis", return_value=None), patch(
        "bot.auto_llm._gather_material_info", return_value=[]
    ), patch("bot.auto_llm._gather_news", return_value=[]), patch(
        "bot.auto_llm._resolve_presentation_block", return_value=""
    ), patch("bot.auto_llm._load_research_memory_context", return_value=""), patch(
        "bot.auto_llm._try_research_ticker", return_value=result
    ), patch(
        "bot.auto_llm._build_source_sections",
        return_value={"evidence": [evidence]},
    ), patch("bot.auto_llm._save_cache"):
        payload = auto_analyze_ticker(
            "2330",
            root=tmp_path,
            client=client,
            force_refresh=True,
            enable_calendar=False,
            enable_web_search=False,
            enable_presentation_download=False,
            enable_quant=False,
        )

    assert payload is not None
    assert payload["evidence_ids"] == [evidence["id"]]
    assert db.get_evidence(evidence["id"]) is not None
    row = db.list_llm_analysis(prompt_id="research_ticker", limit=1)[0]
    assert json.loads(row.evidence_json) == [evidence["id"]]
    assert db.list_memory_items(memory_layer="long") == []


def test_long_memory_gate_keeps_only_exact_registered_evidence(tmp_path: Path) -> None:
    from bot.source_registry import SourceRegistry

    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document(
            "fixture",
            b"exact raw evidence",
            source_ref="fixture:1",
            source_url="https://example.test/fixture/1",
            title="Fixture source",
            suffix="txt",
            mime_type="text/plain",
        )
    exact = make_evidence(
        "official",
        source="fixture",
        source_ref="fixture:1",
        title="Fixture source",
        excerpt="exact raw evidence",
        source_document_id=source.document_id,
        provenance_status="exact",
    )
    unresolved = make_evidence(
        "news",
        source="cache",
        source_ref="cache:1",
        title="Unresolved cache",
        excerpt="可能的摘要",
    )
    db = StockDB.open(root=tmp_path)
    persist_evidence_items(db, [exact, unresolved])

    accepted, rejected = _select_verified_long_memory_evidence(
        db, [exact, unresolved],
    )
    assert [item["id"] for item in accepted] == [exact["id"]]
    assert rejected == [unresolved["id"]]


def test_research_memory_context_closes_database_connection(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    original_close = db.close
    close = MagicMock(wraps=original_close)
    db.close = close  # type: ignore[method-assign]

    with patch("bot.stock_db.StockDB.open", return_value=db):
        assert _load_research_memory_context("2330", root=tmp_path) == ""

    close.assert_called_once_with()


def test_build_source_sections_passes_root_and_reuses_mops_exact_evidence(
    tmp_path: Path,
) -> None:
    from bot.mops_research import materialize_mops_detail_slice
    from bot.source_registry import SourceRegistry

    detail_url = "https://example.test/mops/detail/auto"
    raw_text = "重大訊息原文：資本支出與營運說明"
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document(
            "mops_material_detail",
            raw_text.encode("utf-8"),
            source_ref=detail_url,
            source_url=detail_url,
            title="MOPS 測試詳情",
            suffix="html",
            mime_type="text/html",
            as_of="2026-09-18",
        )
    slice_row = materialize_mops_detail_slice(
        root=tmp_path,
        source_document_id=source.document_id,
        canonical_text=raw_text,
        detail_url=detail_url,
        ticker="2330",
        title="MOPS 測試詳情",
    )

    material = SimpleNamespace(
        date=dt.date(2026, 9, 18),
        time="10:00",
        subject="營運說明",
        detail_url=detail_url,
    )
    detail_result = SimpleNamespace(
        text=raw_text,
        evidence_id=slice_row.evidence_id,
    )
    with patch("bot.mops_scraper.ingest_material_detail", return_value=detail_result):
        sections = _build_source_sections(
            "2330",
            root=tmp_path,
            materials=[material],
            news=[],
            calendar_data={"upcoming": [], "past": []},
            web_material=None,
            pipeline_text="",
            logger=MagicMock(),
        )

    assert sections["evidence"][0]["id"] == slice_row.evidence_id
    assert sections["evidence"][0]["source_document_id"] == source.document_id
    assert sections["evidence"][0]["provenance_status"] == "exact"


def test_research_ticker_prompt_declares_presentation_and_quant_block():
    from bot.prompt_registry import PromptRegistry

    # 避開 singleton 快取，直接從檔案重載
    reg = PromptRegistry()
    p = reg.get("research_ticker")
    assert p is not None
    assert p.version == "1.2"
    names = {i.name for i in p.inputs}
    assert "presentation_block" in names
    assert "quant_block" in names
    rendered = p.render(
        ticker="2330",
        name="台積電",
        today="2026-09-09",
        upcoming_events="(無)",
        past_events="(無)",
        presentation_block="簡報測試原文",
        quant_block="【籌碼面】外資淨買超=1000",
        news_block="(無)",
        web_block="(無)",
    )
    assert "簡報測試原文" in rendered
    assert "法說會簡報" in rendered
    assert "外資淨買超=1000" in rendered
    assert "量化證據" in rendered


def test_format_fundamentals_and_chips_quant():
    from bot.auto_llm import _format_chips_quant, _format_fundamentals_quant
    from bot.fundamentals_fetcher import (
        FundamentalSnapshot,
        MonthlyRevenue,
        ValuationDaily,
    )
    from bot.llm_analyzer import ChipsContext

    fund = FundamentalSnapshot(
        ticker="2330",
        valuation=ValuationDaily(
            ticker="2330", date="2026-09-09",
            pe_ratio=18.5, pb_ratio=5.2, dividend_yield=1.8,
        ),
        revenues=[
            MonthlyRevenue(
                ticker="2330", year=2026, month=8,
                yoy=12.5, mom=3.1, cum_yoy=10.0,
            ),
        ],
    )
    lines = _format_fundamentals_quant(fund)
    text = "\n".join(lines)
    assert "PER=18.5" in text
    assert "YoY=+12.5%" in text

    chip_lines = _format_chips_quant(chips=ChipsContext(
        foreign_net=1000,
        investment_trust_net=-200,
        dealer_net=50,
        margin_buy_change_pct=1.5,
        short_borrow_change_pct=-0.5,
        block_trade_net=0,
        notes="基於最近 5 交易日資料",
    ))
    chip_text = "\n".join(chip_lines)
    assert "外資淨買超=1,000" in chip_text
    assert "投信=-200" in chip_text


def test_build_quant_block_uses_local_cache(tmp_path: Path):
    from bot.auto_llm import _build_quant_block
    from bot.fundamentals_fetcher import FundamentalSnapshot, ValuationDaily

    fake = FundamentalSnapshot(
        ticker="2330",
        valuation=ValuationDaily(
            ticker="2330", date="2026-09-09",
            pe_ratio=22.0, pb_ratio=6.0, dividend_yield=2.0,
        ),
    )
    logger = MagicMock()
    chips = MagicMock()
    chips.foreign_net = 500.0
    chips.investment_trust_net = 100.0
    chips.dealer_net = 0.0
    chips.margin_buy_change_pct = 0.0
    chips.short_borrow_change_pct = 0.0
    chips.block_trade_net = 0.0
    chips.notes = "近 5 日"

    with patch(
        "bot.fundamentals_fetcher.build_fundamental_snapshot",
        return_value=fake,
    ), patch(
        "bot.auto_llm._format_etf_consensus_quant",
        return_value=["【主動 ETF 共識】", "- 被 3 檔持有，合計權重≈8.50%"],
    ):
        out = _build_quant_block(
            "2330", root=tmp_path, logger=logger, chips=chips,
            refresh_fundamentals_if_empty=False,
        )
    assert "PER=22.0" in out
    assert "外資淨買超=500" in out
    assert "被 3 檔持有" in out


def test_research_memory_context_keeps_layer_labels(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    store = MemoryStore(db)
    store.put_short(
        "short context 2330",
        summary="short summary 2330",
        scope_type="ticker",
        scope_id="2330",
        quality_status="ok",
    )
    store.put_long(
        "long research 2330",
        summary="long summary 2330",
        scope_type="ticker",
        scope_id="2330",
        quality_status="ok",
    )
    text = _load_research_memory_context("2330", root=tmp_path)
    assert "【短期記憶】" in text
    assert "【長期記憶】" in text
    assert "short context 2330" in text
    assert "long research 2330" in text

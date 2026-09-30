from __future__ import annotations

from pathlib import Path

from bot.news_fetcher import NewsItem
from bot.pipeline_shared import build_report_evidence, persist_report_evidence
from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB, default_db_path


def test_report_evidence_uses_explicit_source_ids_only(tmp_path: Path) -> None:
    with SourceRegistry(tmp_path) as registry:
        news_doc = registry.capture_document(
            "cnyes",
            b'{"news": "exact"}',
            source_ref="news-1",
            source_url="https://news.example/1",
            as_of="2026-09-20",
        )
        macro_doc = registry.capture_document(
            "market_macro",
            b'{"macro": "exact"}',
            source_ref="macro:2026-09-20",
            source_url="yfinance://macro",
            as_of="2026-09-20",
        )

    packets = build_report_evidence(
        news_items=[NewsItem(
            news_id="news-1",
            title="台股新聞",
            summary="新聞摘要",
            source_document_id=news_doc.document_id,
        )],
        macro={
            "asof_date": "2026-09-20",
            "fetched_at": "2026-09-20T08:00:00+08:00",
            "indices": {"^TWII": {"pct_change": 1.0}},
            "source_document_id": macro_doc.document_id,
        },
    )
    persisted = persist_report_evidence(tmp_path, packets)

    assert len(persisted) == 2
    assert {item["provenance_status"] for item in persisted} == {"exact"}
    assert all(item["source_document_id"].startswith("source-doc:") for item in persisted)

    db = StockDB.open(path=default_db_path(tmp_path))
    try:
        assert all(db.get_evidence(item["id"]) is not None for item in persisted)
    finally:
        db.close()


def test_report_evidence_does_not_guess_missing_source(tmp_path: Path) -> None:
    packets = build_report_evidence(
        news_items=[NewsItem(
            news_id="news-legacy",
            title="舊快取新聞",
            summary="沒有可證明的 raw source",
            source_document_id="source-doc:not-real",
        )],
    )

    persisted = persist_report_evidence(tmp_path, packets)

    assert persisted[0]["source_document_id"] == ""
    assert persisted[0]["provenance_status"] == "unresolved"
    assert persisted[0]["metadata"]["source_identity_check"] == "missing_or_not_replayable"

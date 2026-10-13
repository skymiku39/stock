from __future__ import annotations

from pathlib import Path

from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB
from bot.web_search import SearchResult
from bot.web_search_research import (
    materialize_web_search_slice,
    replay_web_search_document,
)


def _seed_google_news(root: Path) -> tuple[str, SearchResult]:
    payload = (
        "<rss><channel><item>"
        "<title>2330 測試新聞</title>"
        "<link>https://example.test/news</link>"
        "<pubDate>Wed, 16 Sep 2026 12:00:00 GMT</pubDate>"
        "<description>原始搜尋摘要</description>"
        "</item></channel></rss>"
    ).encode("utf-8")
    registry = SourceRegistry(root)
    try:
        document = registry.capture_document(
            "google_news_rss",
            payload,
            source_url="https://news.google.com/rss/search?q=2330",
            source_ref="2330 台積電",
            title="Google News RSS: 2330 台積電",
            kind="search_response",
            suffix="xml",
            mime_type="application/rss+xml",
            retrieved_at="2026-09-16T12:01:00+08:00",
        )
    finally:
        registry.close()
    return document.document_id, SearchResult(
        title="2330 測試新聞",
        url="https://example.test/news",
        snippet="原始搜尋摘要",
        source="google_news",
        published_at="2026-09-16T12:00:00",
        source_document_id=document.document_id,
    )


def test_web_search_slice_materializes_and_is_idempotent(tmp_path: Path) -> None:
    source_id, item = _seed_google_news(tmp_path)
    first = materialize_web_search_slice(
        root=tmp_path,
        source_document_id=source_id,
        items=[item],
    )
    second = materialize_web_search_slice(
        root=tmp_path,
        source_document_id=source_id,
        items=[item],
    )

    assert first.status == "ok"
    assert second.status == "reused"
    assert first.research_document_id == second.research_document_id
    assert first.evidence_ids == second.evidence_ids
    assert first.claim_ids == second.claim_ids

    db = StockDB.open(root=tmp_path)
    try:
        evidence = db.get_evidence(first.evidence_ids[0])
        claim = db.get_claim(first.claim_ids[0])
        assert evidence is not None
        assert evidence.source_document_id == source_id
        assert evidence.kind == "news"
        assert claim is not None and claim.status == "verified"
        assert db.list_claim_evidence(first.claim_ids[0])
        assert db.list_evidence_links(first.evidence_ids[0])
    finally:
        db.close()


def test_web_search_slice_rejects_tampered_raw(tmp_path: Path) -> None:
    source_id, item = _seed_google_news(tmp_path)
    db = StockDB.open(root=tmp_path)
    try:
        source = db.get_source_document(source_id)
        assert source is not None
        (tmp_path / source.content_path).write_bytes(b"tampered")
    finally:
        db.close()

    result = materialize_web_search_slice(
        root=tmp_path,
        source_document_id=source_id,
        items=[item],
    )
    assert result.status == "failed"
    assert "hash mismatch" in result.error


def test_web_search_slice_replays_exact_raw_without_guessing(tmp_path: Path) -> None:
    source_id, _item = _seed_google_news(tmp_path)
    result = replay_web_search_document(
        root=tmp_path,
        source_document_id=source_id,
    )
    assert result.status == "ok"
    assert result.evidence_ids
    assert result.claim_ids


def test_web_search_slice_does_not_accept_other_source_domains(tmp_path: Path) -> None:
    registry = SourceRegistry(tmp_path)
    try:
        document = registry.capture_document(
            "web_page",
            b"<html><title>not a search response</title></html>",
            source_ref="https://example.test/page",
            kind="page_response",
            suffix="html",
            mime_type="text/html",
        )
    finally:
        registry.close()

    result = materialize_web_search_slice(
        root=tmp_path,
        source_document_id=document.document_id,
        items=[{"title": "不應建立", "url": "https://example.test/page"}],
    )
    assert result.status == "failed"
    assert "unsupported search source" in result.error

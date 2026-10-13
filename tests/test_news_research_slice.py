from __future__ import annotations

import json
from pathlib import Path

from bot.news_research import materialize_news_cache_slice
from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB


def _seed_news_source(root: Path) -> tuple[str, list[dict]]:
    items = [{
        "news_id": "n-1",
        "title": "2330 測試新聞標題",
        "summary": "這是原始摘要。",
        "url": "https://news.example/n-1",
        "source": "cnyes",
        "category": "台股",
        "published_at": "2026-09-19T10:00:00+08:00",
        "related_tickers": ["2330"],
    }]
    payload = json.dumps(
        {"asof": "2026-09-19", "items": items},
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    registry = SourceRegistry(root)
    try:
        document = registry.capture_document(
            "cnyes",
            payload,
            source_url="https://api.cnyes.com/media/api/v1/newslist/category/tw_stock",
            source_ref="news:2026-09-19",
            title="鉅亨台股新聞 2026-09-19",
            kind="news_cache",
            suffix="json",
            mime_type="application/json",
            as_of="2026-09-19",
            retrieved_at="2026-09-19T10:01:00+08:00",
        )
        return document.document_id, items
    finally:
        registry.close()


def test_news_slice_materializes_and_is_idempotent(tmp_path: Path) -> None:
    source_id, items = _seed_news_source(tmp_path)
    first = materialize_news_cache_slice(
        root=tmp_path,
        source_document_id=source_id,
        items=items,
    )
    second = materialize_news_cache_slice(
        root=tmp_path,
        source_document_id=source_id,
        items=items,
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
        assert claim is not None
        assert claim.status == "verified"
        assert db.list_claim_evidence(first.claim_ids[0])
        assert db.list_evidence_links(first.evidence_ids[0])
    finally:
        db.close()


def test_news_slice_rejects_unknown_source(tmp_path: Path) -> None:
    result = materialize_news_cache_slice(
        root=tmp_path,
        source_document_id="source-doc:not-real",
        items=[{"title": "不應建立"}],
    )
    assert result.status == "failed"
    assert "unknown source_document_id" in result.error

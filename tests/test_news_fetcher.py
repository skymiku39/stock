from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bot.news_fetcher import (
    NewsItem,
    _parse_cnyes_item,
    fetch_today_news,
    load_cached_news_only,
)
from bot.stock_db import StockDB


def test_news_refresh_registers_raw_cache_manifest(tmp_path: Path) -> None:
    items = [NewsItem(
        news_id="n-1",
        title="2330 測試新聞",
        summary="測試摘要",
        url="https://news.example/n-1",
        published_at="2026-09-16T10:00:00+08:00",
        related_tickers=["2330"],
    )]
    with patch("bot.news_fetcher.fetch_cnyes", return_value=items), patch(
        "bot.news_fetcher.mirror_file_to_cloud",
    ):
        fetched = fetch_today_news(
            root=tmp_path,
            force_refresh=True,
            limit=10,
        )

    assert fetched == items
    assert fetched[0].source_document_id.startswith("source-doc:cnyes:")
    cached = load_cached_news_only(root=tmp_path)
    assert cached[0].news_id == "n-1"
    assert cached[0].source_document_id == fetched[0].source_document_id

    from bot.ask_adapters import collect_snapshot_resource_evidence
    evidence = collect_snapshot_resource_evidence(
        "2330", root=tmp_path, resource_needs=["news"],
    )
    assert evidence[0]["source_document_id"] == fetched[0].source_document_id
    assert evidence[0]["metadata"]["pipeline"] == "news_official_cache_slice"

    db = StockDB.open(path=tmp_path / "data" / "stock.db")
    documents = db.list_source_documents(source="cnyes", kind="news_cache")
    assert len(documents) == 1
    document = documents[0]
    raw_path = tmp_path / document.content_path
    assert raw_path.exists()
    assert raw_path.read_bytes() == (
        tmp_path / "data" / "news" / f"news_{document.as_of}.json"
    ).read_bytes()
    assert document.content_hash
    assert document.as_of
    assert document.retrieved_at
    runs = db.list_ingestion_runs(source="cnyes")
    assert len(runs) == 1
    assert runs[0].status == "ok"
    assert runs[0].document_count == 1


def test_news_research_slice_failure_marks_ingestion_partial(tmp_path: Path) -> None:
    items = [NewsItem(
        news_id="n-fail",
        title="2330 失敗切片新聞",
        summary="測試摘要",
        url="https://news.example/n-fail",
        published_at="2026-09-16T10:00:00+08:00",
        related_tickers=["2330"],
    )]
    failed = type("Slice", (), {"status": "failed", "error": "slice boom"})()
    with patch("bot.news_fetcher.fetch_cnyes", return_value=items), patch(
        "bot.news_fetcher.mirror_file_to_cloud",
    ), patch(
        "bot.news_research.materialize_news_cache_slice",
        return_value=failed,
    ):
        fetched = fetch_today_news(
            root=tmp_path,
            force_refresh=True,
            limit=10,
        )

    assert fetched == items
    cached = load_cached_news_only(root=tmp_path)
    assert cached[0].news_id == "n-fail"
    db = StockDB.open(path=tmp_path / "data" / "stock.db")
    documents = db.list_source_documents(source="cnyes", kind="news_cache")
    assert len(documents) == 1
    assert (tmp_path / documents[0].content_path).exists()
    runs = db.list_ingestion_runs(source="cnyes")
    assert len(runs) == 1
    assert runs[0].status == "partial"
    assert "slice boom" in runs[0].error


def test_cnyes_publish_time_is_normalized_to_taipei_timezone() -> None:
    item = _parse_cnyes_item({
        "id": "n-2",
        "title": "時間測試",
        "publishAt": 0,
    })
    assert item is not None
    assert item.published_at.endswith("+08:00")

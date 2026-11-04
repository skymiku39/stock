from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bot.news_fetcher import (
    NewsItem,
    _parse_cnyes_item,
    _save_cache,
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


def test_restored_news_cache_uses_its_exact_source_version(tmp_path, monkeypatch):
    from bot import news_fetcher

    monkeypatch.setattr(news_fetcher, "mirror_file_to_cloud", lambda *a, **kw: None)
    monkeypatch.setattr(news_fetcher, "restore_file_from_cloud", lambda *a, **kw: None)
    day = dt.date(2026, 11, 4)
    old = [NewsItem(news_id="same-id", title="Original headline")]
    path = _save_cache(old, day, tmp_path)
    original_bytes = path.read_bytes()
    newer = [NewsItem(news_id="same-id", title="Corrected headline")]
    _save_cache(newer, day, tmp_path)
    db = StockDB.open(root=tmp_path)
    try:
        db.conn.execute("UPDATE source_documents SET retrieved_at=? WHERE document_id=?",
                        ("2026-11-04T18:00:00+08:00", newer[0].source_document_id))
        db.conn.commit()
    finally:
        db.close()

    path.write_bytes(original_bytes)
    restored = load_cached_news_only(root=tmp_path, date=day)
    assert restored[0].title == "Original headline"
    assert restored[0].source_document_id == old[0].source_document_id
    assert restored[0].source_document_id != newer[0].source_document_id


@pytest.mark.parametrize("embedded_id", [False, True])
def test_modified_news_cache_cannot_borrow_a_registered_source(tmp_path, monkeypatch, embedded_id):
    from bot import news_fetcher

    monkeypatch.setattr(news_fetcher, "mirror_file_to_cloud", lambda *a, **kw: None)
    monkeypatch.setattr(news_fetcher, "restore_file_from_cloud", lambda *a, **kw: None)
    day = dt.date(2026, 11, 4)
    items = [NewsItem(news_id="same-id", title="Original headline")]
    path = _save_cache(items, day, tmp_path)
    payload = json.loads(path.read_bytes())
    payload["items"][0]["title"] = "Unregistered change"
    if embedded_id:
        payload["items"][0]["source_document_id"] = items[0].source_document_id
    path.write_text(json.dumps(payload), encoding="utf-8")

    cached = load_cached_news_only(root=tmp_path, date=day)
    assert cached[0].title == "Unregistered change"
    assert cached[0].source_document_id == ""

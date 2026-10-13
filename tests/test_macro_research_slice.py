from __future__ import annotations

import json
from pathlib import Path

from bot.macro_research import materialize_macro_snapshot_slice
from bot.market_macro import macro_quality_status
from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB


def _snapshot() -> dict:
    return {
        "fetched_at": "2026-09-19T10:00:00+08:00",
        "asof_date": "2026-09-19",
        "usdtwd": 31.2,
        "source_date_status": "aligned",
        "source_dates": {"taiex": "2026-09-19"},
        "indices": {"^SOX": {"price": 6000.0}},
        "stocks": {"NVDA": {"price": 180.0}},
        "adr_premiums": [{"symbol": "TSM", "premium_pct": 1.2}],
        "notes": ["fixture"],
    }


def test_macro_quality_downgrades_temporal_and_cached_inputs() -> None:
    snapshot = _snapshot()
    assert macro_quality_status(snapshot) == "partial"  # fixture note is explicit

    snapshot["notes"] = []
    assert macro_quality_status(snapshot) == "ok"

    snapshot["source_date_status"] = "mixed"
    assert macro_quality_status(snapshot) == "partial"

    snapshot["source_date_status"] = "aligned"
    snapshot["futures_basis"] = {
        "quality_status": "partial",
        "cached": True,
    }
    assert macro_quality_status(snapshot) == "partial"


def test_macro_slice_materializes_and_reuses(tmp_path: Path) -> None:
    snapshot = _snapshot()
    registry = SourceRegistry(tmp_path)
    try:
        document = registry.capture_document(
            "market_macro",
            json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8"),
            source_url="yfinance://macro",
            source_ref="macro:2026-09-19",
            title="Market macro snapshot 2026-09-19",
            kind="snapshot",
            suffix="json",
            mime_type="application/json",
            as_of="2026-09-19",
            retrieved_at="2026-09-19T10:00:00+08:00",
        )
    finally:
        registry.close()

    first = materialize_macro_snapshot_slice(
        root=tmp_path,
        source_document_id=document.document_id,
        snapshot=snapshot,
    )
    second = materialize_macro_snapshot_slice(
        root=tmp_path,
        source_document_id=document.document_id,
        snapshot=snapshot,
    )
    assert first.status == "ok"
    assert second.status == "reused"
    assert first.evidence_id == second.evidence_id
    assert len(first.claim_ids) == 5

    db = StockDB.open(root=tmp_path)
    try:
        evidence = db.get_evidence(first.evidence_id)
        assert evidence is not None
        assert evidence.source_document_id == document.document_id
        assert evidence.quality_status == "partial"
        refreshed_source = db.get_source_document(document.document_id)
        assert refreshed_source is not None
        assert refreshed_source.quality_status == "partial"
        assert all(db.get_claim(claim_id).status == "verified" for claim_id in first.claim_ids)
        assert len(db.list_evidence_links(first.evidence_id)) == 1
    finally:
        db.close()


def test_macro_slice_replays_from_cloud_raw_when_local_archive_is_missing(
    tmp_path: Path, monkeypatch,
) -> None:
    """研究流程必須能在另一份工作區以 exact manifest 重播 raw。"""
    snapshot = _snapshot()
    producer = tmp_path / "producer"
    consumer = tmp_path / "consumer"
    cloud = tmp_path / "cloud-cache"
    monkeypatch.setenv("GOOGLE_CACHE_DIR", str(cloud))

    with SourceRegistry(producer) as registry:
        document = registry.capture_document(
            "market_macro",
            json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8"),
            source_url="yfinance://macro",
            source_ref="macro:2026-09-19",
            title="Market macro snapshot 2026-09-19",
            kind="snapshot",
            suffix="json",
            mime_type="application/json",
            as_of="2026-09-19",
            retrieved_at="2026-09-19T10:00:00+08:00",
        )

    # 只把 exact manifest 帶到另一個工作區；consumer 的本地 raw 刻意不存在。
    consumer_db = StockDB.open(root=consumer)
    try:
        consumer_db.upsert_source_document(document)
    finally:
        consumer_db.close()

    result = materialize_macro_snapshot_slice(
        root=consumer,
        source_document_id=document.document_id,
        snapshot=snapshot,
    )

    assert result.status == "ok"
    assert (consumer / document.content_path).read_bytes() == (
        producer / document.content_path
    ).read_bytes()
    db = StockDB.open(root=consumer)
    try:
        evidence = db.get_evidence(result.evidence_id)
        assert evidence is not None
        assert evidence.source_document_id == document.document_id
        assert all(db.get_claim(claim_id).status == "verified" for claim_id in result.claim_ids)
    finally:
        db.close()

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from bot.chips_fetcher import _fetch_endpoint
from bot.evidence import (
    make_evidence,
    normalize_evidence_packet,
    resolve_provenance_status,
)
from bot.market_macro import MacroSnapshot, _save_cache
from bot.stock_db import StockDB


class _Response:
    status_code = 200

    def __init__(self, payload: dict, raw: bytes):
        self.payload = payload
        self.content = raw

    def json(self):
        return self.payload


class _Session:
    def __init__(self, response: _Response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response


def test_chips_endpoint_captures_original_api_response(tmp_path: Path) -> None:
    payload = {"fields": ["證券代號"], "data": [["2330"]]}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = _fetch_endpoint(
        "qfii_daily",
        dt.date(2026, 9, 17),
        session=_Session(_Response(payload, raw)),
        root=tmp_path,
        use_cache=False,
    )

    assert result == payload
    db = StockDB.open(root=tmp_path)
    docs = db.list_source_documents(source="chips_twse_qfii_daily")
    assert len(docs) == 1
    assert (tmp_path / docs[0].content_path).read_bytes() == raw
    assert db.get_source_health("chips_twse_qfii_daily").last_status == "ok"


def test_macro_cache_is_registered_as_replayable_snapshot(tmp_path: Path) -> None:
    _save_cache(MacroSnapshot(
        fetched_at="2026-09-17T10:00:00+08:00",
        asof_date="2026-09-17",
        notes=["fixture"],
    ), tmp_path)

    db = StockDB.open(root=tmp_path)
    docs = db.list_source_documents(source="market_macro", kind="snapshot")
    assert len(docs) == 1
    cached = tmp_path / "data" / "macro" / "macro_2026-09-17.json"
    assert cached.exists()
    assert (tmp_path / docs[0].content_path).read_bytes() == cached.read_bytes()


def test_exact_label_without_source_document_identity_does_not_survive() -> None:
    """明確 exact／composite_exact 缺少身分時不得倖存；純 resolver 不能證明回放。"""
    assert (
        resolve_provenance_status(provenance_status="exact") == "unresolved"
    )
    assert (
        resolve_provenance_status(provenance_status="composite_exact")
        == "unresolved"
    )
    assert (
        resolve_provenance_status(
            provenance_status="exact",
            metadata={"cache_only": True},
        )
        == "cache_only"
    )
    assert (
        resolve_provenance_status(
            provenance_status="unresolved",
            source_document_id="source-doc:fixture",
        )
        == "unresolved"
    )
    assert (
        resolve_provenance_status(
            provenance_status="composite_exact",
            metadata={"source_document_ids": ["source-doc:a", "source-doc:b"]},
        )
        == "composite_exact"
    )
    assert (
        resolve_provenance_status(
            source_document_id="source-doc:fixture",
            provenance_status="exact",
        )
        == "exact"
    )

    packet = make_evidence(
        "news",
        source="fixture",
        title="no identity",
        excerpt="body",
        provenance_status="exact",
    )
    assert packet["provenance_status"] == "unresolved"
    assert not packet["source_document_id"]

    normalized = normalize_evidence_packet({
        "kind": "news",
        "source": "fixture",
        "title": "legacy exact claim",
        "excerpt": "body",
        "provenance_status": "composite_exact",
        "source_document_id": "",
        "metadata": {"provenance_status": "composite_exact"},
    })
    assert normalized["provenance_status"] == "unresolved"
    assert normalized["metadata"]["provenance_status"] == "unresolved"

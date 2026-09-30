from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB
from bot.taifex_research import materialize_taifex_slice


def _capture(tmp_path: Path, content: bytes):
    with SourceRegistry(tmp_path) as registry:
        return registry.capture_document(
            "taifex_daily_fut",
            content,
            source_url="https://openapi.taifex.com.tw/v1/DailyMarketReportFut",
            source_ref="taifex:2026-09-18:202610",
            title="TAIFEX TX daily futures 2026-09-18",
            kind="http_response",
            suffix="csv",
            mime_type="text/csv; charset=utf-8",
            as_of="20260918",
        )


def test_taifex_slice_materializes_and_reuses(tmp_path: Path) -> None:
    raw = (
        "\ufeff日期,契約代號,到期月份(週別),最後成交價,結算價,未沖銷契約數\n"
        "20260918,TX,202610,22100,22090,90000\n"
        "20260918,TX,202610W2,22120,22110,1000\n"
    ).encode("utf-8")
    document = _capture(tmp_path, raw)
    first = materialize_taifex_slice(
        root=tmp_path,
        source_document_id=document.document_id,
        contract_month="202610",
        futures_price=22100,
        spot_price=22000,
        basis=100,
        basis_pct=0.455,
        open_interest=90000,
        state="正價差",
        asof_date="20260918",
    )
    second = materialize_taifex_slice(
        root=tmp_path,
        source_document_id=document.document_id,
        contract_month="202610",
        futures_price=22100,
        spot_price=22000,
        basis=100,
        basis_pct=0.455,
        open_interest=90000,
        state="正價差",
        asof_date="20260918",
    )
    assert first.status == "ok"
    assert second.status == "reused"
    assert first.research_document_id == second.research_document_id
    assert len(first.claim_ids) == 5

    db = StockDB.open(root=tmp_path)
    try:
        research = db.get_research_document(first.research_document_id)
        assert research is not None
        assert research.source_document_id == document.document_id
        evidence = db.get_evidence(first.evidence_id)
        assert evidence is not None
        assert evidence.source_document_id == document.document_id
        assert "計算價差" not in evidence.excerpt
        assert len(db.list_evidence_links(first.evidence_id)) == 1
        assert all(db.get_claim(claim_id).status == "verified" for claim_id in first.claim_ids)
    finally:
        db.close()


def test_taifex_slice_rejects_tampered_raw(tmp_path: Path) -> None:
    raw = json.dumps([
        {
            "Contract": "TX",
            "ContractMonth(Week)": "202610",
            "Last": "22100",
            "SettlementPrice": "22090",
            "OpenInterest": "90000",
            "Date": "20260918",
        }
    ], ensure_ascii=False).encode("utf-8")
    document = _capture(tmp_path, raw)
    path = tmp_path / document.content_path
    path.write_bytes(raw + b"tampered")
    result = materialize_taifex_slice(
        root=tmp_path,
        source_document_id=document.document_id,
        contract_month="202610",
        futures_price=22100,
        open_interest=90000,
        asof_date="20260918",
    )
    assert result.status == "failed"
    assert "hash mismatch" in result.error


def test_fetch_tx_futures_basis_materializes_research_slice(tmp_path: Path) -> None:
    from bot.market_macro import fetch_tx_futures_basis

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = json.dumps([
            {
                "Contract": "TX",
                "ContractMonth(Week)": "202610",
                "Last": "22100",
                "SettlementPrice": "22090",
                "OpenInterest": "90000",
                "Date": "20260918",
            }
        ]).encode("utf-8")

        def json(self):
            return json.loads(self.content.decode("utf-8"))

    with patch("requests.get", return_value=Response()):
        basis = fetch_tx_futures_basis(22000, root=tmp_path)
    assert basis is not None
    assert basis.source_document_id
    db = StockDB.open(root=tmp_path)
    try:
        documents = db.list_research_documents(limit=20)
        assert any(
            row.document_type == "taifex_futures"
            and row.source_document_id == basis.source_document_id
            for row in documents
        )
    finally:
        db.close()


def test_taifex_published_at_does_not_fallback_to_observed_date(tmp_path: Path) -> None:
    """TAIFEX 行情日屬 as_of／observed_at；缺上游 published_at 時不可回填。"""
    raw = (
        "\ufeff日期,契約代號,到期月份(週別),最後成交價,結算價,未沖銷契約數\n"
        "20260918,TX,202610,22100,22090,90000\n"
    ).encode("utf-8")
    document = _capture(tmp_path, raw)
    result = materialize_taifex_slice(
        root=tmp_path,
        source_document_id=document.document_id,
        contract_month="202610",
        futures_price=22100,
        spot_price=22000,
        basis=100,
        basis_pct=0.455,
        open_interest=90000,
        state="正價差",
        asof_date="20260918",
    )
    assert result.status == "ok"
    db = StockDB.open(root=tmp_path)
    try:
        research = db.get_research_document(result.research_document_id)
        evidence = db.get_evidence(result.evidence_id)
        assert research is not None and evidence is not None
        assert research.published_at == ""
        assert evidence.published_at == ""
        assert evidence.as_of in {"20260918", "2026-09-18"}
        assert evidence.observed_at in {"20260918", "2026-09-18"}
        assert evidence.retrieved_at
    finally:
        db.close()

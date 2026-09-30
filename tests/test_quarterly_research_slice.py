import json
from pathlib import Path

from bot.fundamentals_fetcher import fetch_quarterly_financials
from bot.stock_db import StockDB


PAYLOAD = [{
    "公司代號": "2330",
    "年度": "115",
    "季別": "2",
    "營業收入": "1000000",
    "營業毛利": "400000",
    "營業利益": "200000",
    "本期淨利": "150000",
    "每股盈餘": "5.00",
}]


class FakeResponse:
    status_code = 200

    def __init__(self):
        self.content = json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")

    def json(self):
        return PAYLOAD


class FakeSession:
    def get(self, url, timeout=20):
        return FakeResponse()


def test_quarterly_refresh_wires_raw_canonical_research_and_claims(tmp_path: Path):
    rows = fetch_quarterly_financials(
        "2330", root=tmp_path, session=FakeSession(),
    )

    assert len(rows) == 1
    assert rows[0].source_document_id.startswith("source-doc:")
    assert rows[0].quality_status == "ok"

    db = StockDB.open(root=tmp_path)
    canonical = db.get_quarterly_report("2330", "2026Q2")
    assert len(canonical) == 1
    assert canonical[0].source_record_id == rows[0].source_document_id
    assert db.get_source_document(rows[0].source_document_id) is not None

    evidence = [
        item for item in db.list_evidence(limit=1000)
        if item.kind == "quarterly_report"
    ]
    assert len(evidence) == 1
    assert evidence[0].source_document_id == rows[0].source_document_id
    assert db.conn.execute(
        "SELECT COUNT(*) FROM claims WHERE run_id LIKE 'quarterly-slice:%'"
    ).fetchone()[0] == 6


def test_quarterly_refresh_is_idempotent_with_cached_raw_metadata(tmp_path: Path):
    first = fetch_quarterly_financials(
        "2330", root=tmp_path, session=FakeSession(),
    )
    second = fetch_quarterly_financials(
        "2330", root=tmp_path, session=FakeSession(),
    )

    assert first[0].source_document_id == second[0].source_document_id
    db = StockDB.open(root=tmp_path)
    assert db.conn.execute(
        "SELECT COUNT(*) FROM quarterly_report WHERE symbol='2330'"
    ).fetchone()[0] == 1
    assert db.conn.execute(
        "SELECT COUNT(*) FROM evidence_items WHERE kind='quarterly_report'"
    ).fetchone()[0] == 1


def test_quarterly_published_at_does_not_fallback_to_period(tmp_path: Path) -> None:
    """季報期間只能寫在 as_of；缺發布時間時 published_at 必須空白。"""
    from bot.quarterly_research import materialize_quarterly_slice
    from bot.source_registry import SourceRegistry
    from bot.stock_db import QuarterlyReport

    payload = json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document(
            "quarterly_twse",
            payload,
            source_ref="quarterly:test",
            suffix="json",
            as_of="2026Q2",
            retrieved_at="2026-09-18T10:00:00+08:00",
            quality_status="ok",
        )
    report = QuarterlyReport(
        symbol="2330",
        period="2026Q2",
        eps=5.0,
        revenue=1000000.0,
        gross_margin=40.0,
        op_margin=20.0,
        net_margin=15.0,
        source_date="2026Q2",
        retrieved_at="2026-09-18T10:00:00+08:00",
        quality_status="ok",
        source_record_id=source.document_id,
    )
    result = materialize_quarterly_slice(root=tmp_path, report=report)
    assert result.status == "ok"
    db = StockDB.open(root=tmp_path)
    try:
        research = db.get_research_document(result.research_document_id)
        evidence = db.get_evidence(result.evidence_id)
        assert research is not None and evidence is not None
        assert research.published_at == ""
        assert evidence.published_at == ""
        assert evidence.as_of == "2026Q2"
        assert evidence.observed_at == "2026Q2"
        assert evidence.retrieved_at == "2026-09-18T10:00:00+08:00"
    finally:
        db.close()

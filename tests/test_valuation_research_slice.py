import json
from pathlib import Path

from bot.fundamentals_fetcher import fetch_valuation
from bot.stock_db import StockDB


class FakeResponse:
    status_code = 200

    def __init__(self, rows):
        self._rows = rows
        self.content = json.dumps(rows, ensure_ascii=False).encode("utf-8")

    def json(self):
        return self._rows


class FakeSession:
    def get(self, url, timeout=20):
        if "BWIBBU_ALL" in url:
            return FakeResponse([{
                "證券代號": "2330",
                "證券名稱": "台積電",
                "本益比": "28.52",
                "股價淨值比": "9.92",
                "殖利率(%)": "0.89",
                "日期": "115/09/18",
            }])
        return FakeResponse([{
            "SecuritiesCompanyCode": "6488",
            "CompanyName": "環球晶",
            "PriceEarningRatio": "20",
            "PriceBookRatio": "2",
            "YieldRatio": "1",
            "Date": "20260918",
        }])


def test_valuation_refresh_wires_canonical_research_and_claims(tmp_path: Path):
    valuation = fetch_valuation("2330", root=tmp_path, session=FakeSession())

    assert valuation is not None
    assert valuation.source_document_id.startswith("source-doc:")
    assert valuation.quality_status == "ok"

    db = StockDB.open(root=tmp_path)
    rows = db.get_valuation("2330", "2026-09-18")
    assert len(rows) == 1
    assert rows[0].source_record_id == valuation.source_document_id
    evidence = [
        item for item in db.list_evidence(limit=1000)
        if item.kind == "valuation_daily"
    ]
    assert len(evidence) == 1
    assert evidence[0].source_document_id == valuation.source_document_id
    assert db.conn.execute(
        "SELECT COUNT(*) FROM claims WHERE run_id LIKE 'valuation-slice:%'"
    ).fetchone()[0] == 4


def test_valuation_published_at_does_not_fallback_to_as_of(tmp_path: Path) -> None:
    """估值 as_of 是資料日；缺 source_date／發布時間時不可回填 published_at。"""
    from bot.source_registry import SourceRegistry
    from bot.stock_db import ValuationDaily
    from bot.valuation_research import materialize_valuation_slice

    raw = json.dumps([{"證券代號": "2330"}], ensure_ascii=False).encode("utf-8")
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document(
            "valuation_twse",
            raw,
            source_ref="valuation:test",
            suffix="json",
            as_of="2026-09-18",
            retrieved_at="2026-09-18T10:00:00+08:00",
            quality_status="ok",
        )
    valuation = ValuationDaily(
        symbol="2330",
        as_of="2026-09-18",
        pe_ratio=28.5,
        pb_ratio=9.9,
        dividend_yield=0.89,
        source_date="",
        retrieved_at="2026-09-18T10:00:00+08:00",
        quality_status="ok",
        source_record_id=source.document_id,
    )
    result = materialize_valuation_slice(root=tmp_path, valuation=valuation)
    assert result.status == "ok"
    db = StockDB.open(root=tmp_path)
    try:
        research = db.get_research_document(result.research_document_id)
        evidence = db.get_evidence(result.evidence_id)
        assert research is not None and evidence is not None
        assert research.published_at == ""
        assert evidence.published_at == ""
        assert evidence.as_of == "2026-09-18"
        assert evidence.observed_at == "2026-09-18"
        assert evidence.retrieved_at == "2026-09-18T10:00:00+08:00"
    finally:
        db.close()

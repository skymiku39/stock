import json
from pathlib import Path

from bot.fundamentals_fetcher import fetch_dividends
from bot.stock_db import StockDB


TWSE_ROWS = [{
    "公司代號": "2330",
    "股利年度": "115",
    "股東配發-盈餘分配之現金股利(元/股)": "14",
    "股東配發-法定盈餘公積發放之現金(元/股)": "0",
    "股東配發-資本公積發放之現金(元/股)": "0",
    "股東配發-盈餘轉增資配股(元/股)": "0",
    "股東配發-法定盈餘公積轉增資配股(元/股)": "0",
    "股東配發-資本公積轉增資配股(元/股)": "0",
}]


class FakeResponse:
    status_code = 200
    text = ""
    encoding = "utf-8"

    def __init__(self, rows=None, csv_text=""):
        self._rows = rows
        self.text = csv_text
        self.content = (
            json.dumps(rows, ensure_ascii=False).encode("utf-8")
            if rows is not None else csv_text.encode("utf-8")
        )

    def json(self):
        return self._rows


class FakeSession:
    def get(self, url, timeout=20):
        if "t187ap45_L" in url:
            return FakeResponse(rows=TWSE_ROWS)
        return FakeResponse(csv_text="公司代號,股利年度\n6488,115\n")


def test_dividend_refresh_wires_canonical_research_and_claims(tmp_path: Path):
    rows = fetch_dividends("2330", root=tmp_path, session=FakeSession())

    assert len(rows) == 1
    assert rows[0].year == 2026
    assert rows[0].source_document_id.startswith("source-doc:")

    db = StockDB.open(root=tmp_path)
    canonical = db.get_dividend_history("2330", 2026)
    assert len(canonical) == 1
    assert canonical[0].source_record_id == rows[0].source_document_id
    evidence = [
        item for item in db.list_evidence(limit=1000)
        if item.kind == "dividend_history"
    ]
    assert len(evidence) == 1
    assert evidence[0].source_document_id == rows[0].source_document_id
    assert db.conn.execute(
        "SELECT COUNT(*) FROM claims WHERE run_id LIKE 'dividend-slice:%'"
    ).fetchone()[0] == 4


def test_dividend_published_at_does_not_fallback_to_year(tmp_path: Path) -> None:
    """股利年度只能寫在 as_of；缺 source_date 時 published_at 必須空白。"""
    from bot.dividend_research import materialize_dividend_slice
    from bot.source_registry import SourceRegistry
    from bot.stock_db import DividendHistory

    raw = json.dumps(TWSE_ROWS, ensure_ascii=False).encode("utf-8")
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document(
            "dividend_twse",
            raw,
            source_ref="dividend:test",
            suffix="json",
            as_of="2026",
            retrieved_at="2026-09-18T10:00:00+08:00",
            quality_status="ok",
        )
    dividend = DividendHistory(
        symbol="2330",
        year=2026,
        cash_dividend=14.0,
        stock_dividend=0.0,
        source_date="",
        retrieved_at="2026-09-18T10:00:00+08:00",
        quality_status="ok",
        source_record_id=source.document_id,
    )
    result = materialize_dividend_slice(root=tmp_path, dividend=dividend)
    assert result.status == "ok"
    db = StockDB.open(root=tmp_path)
    try:
        research = db.get_research_document(result.research_document_id)
        evidence = db.get_evidence(result.evidence_id)
        assert research is not None and evidence is not None
        assert research.published_at == ""
        assert evidence.published_at == ""
        assert evidence.as_of == "2026"
        assert evidence.observed_at == "2026"
        assert evidence.retrieved_at == "2026-09-18T10:00:00+08:00"
    finally:
        db.close()

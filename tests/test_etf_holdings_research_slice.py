from __future__ import annotations

import datetime as dt
from pathlib import Path

from bot.active_etf import ActiveEtf, Holding, HoldingsSnapshot
from bot.etf_holdings_fetcher import (
    _parse_etfinfo_holdings_html,
    fetch_and_save_holdings,
)
from bot.etf_holdings_research import materialize_etf_holdings_slice
from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB


def _fixture(root: Path, *, raw: bytes = "2330 台積電 weight 10.50%".encode("utf-8")) -> tuple[StockDB, str, HoldingsSnapshot]:
    db = StockDB.open(root=root)
    registry = SourceRegistry(root, db=db)
    document = registry.capture_document(
        "etf_holdings_00980A",
        raw,
        source_url="https://example.invalid/00980A",
        source_ref="00980A:2026-09-19",
        title="00980A holdings 2026-09-19",
        suffix="html",
        mime_type="text/html",
        as_of="2026-09-19",
    )
    snapshot = HoldingsSnapshot(
        symbol="00980A",
        date=dt.date(2026, 9, 19),
        holdings=[Holding(ticker="2330", name="台積電", weight_pct=10.5)],
    )
    return db, document.document_id, snapshot


def test_etf_holdings_slice_is_exact_and_idempotent(tmp_path: Path) -> None:
    db, source_id, snapshot = _fixture(tmp_path)
    try:
        first = materialize_etf_holdings_slice(
            root=tmp_path,
            snapshot=snapshot,
            source_document_id=source_id,
            db=db,
        )
        assert first.status == "ok"
        assert first.research_document_id
        assert first.evidence_id
        assert first.claim_ids
        assert db.get_research_document(first.research_document_id).source_document_id == source_id
        assert db.get_evidence(first.evidence_id).source_document_id == source_id
        assert db.list_evidence_links(first.evidence_id)[0].source_document_id == source_id
        assert db.get_claim(first.claim_ids[0]).status == "verified"

        second = materialize_etf_holdings_slice(
            root=tmp_path,
            snapshot=snapshot,
            source_document_id=source_id,
            db=db,
        )
        assert second.status == "reused"
        assert second.research_document_id == first.research_document_id
        assert second.evidence_id == first.evidence_id
        assert second.claim_ids == first.claim_ids
    finally:
        db.close()


def test_etf_holdings_slice_does_not_mark_unmatched_weight_verified(tmp_path: Path) -> None:
    db, source_id, snapshot = _fixture(tmp_path, raw=b"2330 holdings page")
    try:
        result = materialize_etf_holdings_slice(
            root=tmp_path,
            snapshot=snapshot,
            source_document_id=source_id,
            db=db,
        )
        assert result.status == "ok"
        claim = db.get_claim(result.claim_ids[0])
        assert claim is not None
        assert claim.status == "unverified"
        assert claim.claim_type == "derived_fact"
    finally:
        db.close()


def test_etf_holdings_slice_rejects_guessed_source(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    snapshot = HoldingsSnapshot(
        symbol="00980A",
        date=dt.date(2026, 9, 19),
        holdings=[Holding(ticker="2330", weight_pct=10.5)],
    )
    try:
        result = materialize_etf_holdings_slice(
            root=tmp_path,
            snapshot=snapshot,
            source_document_id="",
            db=db,
        )
        assert result.status == "failed"
        assert "exact" in result.error
    finally:
        db.close()


def test_etf_fetcher_connects_saved_snapshot_to_research(
    tmp_path: Path, monkeypatch,
) -> None:
    html = ("<html><body>2330 TSMC weight 10.50% holdings data " * 3 + "</body></html>").encode("utf-8")

    class Response:
        def __init__(self) -> None:
            self.content = html
            self.text = html.decode("utf-8")
            self.headers = {"content-type": "text/html; charset=utf-8"}
            self.apparent_encoding = "utf-8"
            self.encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

    class Session:
        def get(self, _url: str, *, timeout: int):
            assert timeout == 30
            return Response()

    class Client:
        enabled = True

    monkeypatch.setattr(
        "bot.etf_holdings_fetcher.gemini_call",
        lambda *_args, **_kwargs: (
            '[{"ticker":"2330","name":"TSMC","weight_pct":10.5}]',
            {"prompt_id": "test", "prompt_version": "1", "latency_ms": 1},
        ),
    )
    result = fetch_and_save_holdings(
        ActiveEtf(
            symbol="00980A",
            name="Test ETF",
            issuer="Test Issuer",
            holdings_url="https://example.invalid/holdings",
        ),
        Client(),
        snapshot_date=dt.date(2026, 9, 19),
        root=tmp_path,
        session=Session(),
    )
    assert result.success is True
    assert result.source_document_id.startswith("source-doc:")
    assert result.research_status == "ok"
    assert result.research_document_id
    assert result.research_evidence_id

    db = StockDB.open(root=tmp_path)
    try:
        assert db.get_research_document(result.research_document_id) is not None
        assert db.get_evidence(result.research_evidence_id).source_document_id == result.source_document_id
    finally:
        db.close()


def test_etfinfo_public_table_uses_deterministic_parser_without_llm(
    tmp_path: Path,
) -> None:
    html = """
    <html><head><title>00980A 持股 2026-09-18</title></head><body>
      <table>
        <tr><th>代號 名稱</th><th>權重</th><th>股數</th></tr>
        <tr><td><a class="stock-code-link">2330</a><span class="stock-name-sub">台積電</span></td>
            <td>9.63%</td><td>753,000</td></tr>
        <tr><td><a class="stock-code-link">2454</a><span class="stock-name-sub">聯發科</span></td>
            <td>5.91%</td><td>249,000</td></tr>
      </table>
    </body></html>
    """.encode("utf-8")

    class Response:
        content = html
        text = html.decode("utf-8")
        headers = {"content-type": "text/html; charset=utf-8"}
        apparent_encoding = "utf-8"
        encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

    class Session:
        def get(self, _url: str, *, timeout: int):
            assert timeout == 30
            return Response()

    class Client:
        enabled = False

    result = fetch_and_save_holdings(
        ActiveEtf(
            symbol="00980A",
            name="Test ETF",
            issuer="Test Issuer",
            holdings_url="https://www.etfinfo.tw/etf/00980A/holdings",
        ),
        Client(),
        snapshot_date=dt.date(2026, 9, 19),
        root=tmp_path,
        session=Session(),
    )
    assert result.success is True
    assert result.holdings_count == 2
    assert result.snapshot_date == dt.date(2026, 9, 18)
    assert result.prompt_id == "etfinfo_holdings_parser"
    assert result.research_status == "ok"


def test_etfinfo_paged_table_without_payload_is_not_treated_as_complete() -> None:
    html = """
    <html><body><p>1 / 4</p>
      <table>
        <tr><th>代號 名稱</th><th>權重</th><th>股數</th></tr>
        <tr><td><a class="stock-code-link">2330</a><span class="stock-name-sub">台積電</span></td>
            <td>9.63%</td><td>753,000</td></tr>
      </table>
    </body></html>
    """
    assert _parse_etfinfo_holdings_html(html, symbol="00980A") is None


def test_moneydj_etf_top_ten_is_not_sent_to_llm(tmp_path: Path, monkeypatch) -> None:
    html = ("""
    <html><body><p>資料日期：2026/09/18 持股明細資料來源內容。</p>
      <table>
        <tr><th>個股名稱</th><th>投資比例(%)</th><th>持有股數</th></tr>
        <tr><td>NVIDIA(NVDA.US)</td><td>8.59</td><td>83,000</td></tr>
        <tr><td>Apple(AAPL.US)</td><td>7.98</td><td>50,100</td></tr>
      </table>
    </body></html>
    """ + " top holdings source text" * 4).encode("utf-8")

    class Response:
        content = html
        text = html.decode("utf-8")
        headers = {"content-type": "text/html; charset=utf-8"}
        apparent_encoding = "utf-8"
        encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

    class Session:
        def get(self, _url: str, *, timeout: int):
            assert timeout == 30
            return Response()

    class Client:
        enabled = True

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("top-N MoneyDJ page must not call LLM")

    monkeypatch.setattr("bot.etf_holdings_fetcher.gemini_call", fail_if_called)
    result = fetch_and_save_holdings(
        ActiveEtf(
            symbol="00402A",
            name="Test ETF",
            issuer="Test Issuer",
            holdings_url="https://www.moneydj.com/etf/x/basic/basic0007.xdjhtm?etfid=00402A.tw",
        ),
        Client(),
        snapshot_date=dt.date(2026, 9, 19),
        root=tmp_path,
        session=Session(),
    )
    assert result.success is False
    assert result.error == "source_incomplete_top_n"
    assert result.prompt_id == ""
    assert result.source_document_id.startswith("source-doc:")


def test_etfinfo_nuxt_payload_keeps_complete_holdings_without_llm(
    tmp_path: Path,
) -> None:
    payload = [None] * 19
    payload[1] = {"data": 2}
    payload[2] = ["ShallowReactive", 3]
    payload[3] = {"etf-detail-base-00980A": 4}
    payload[4] = {"holdings": 5}
    payload[5] = {"snapshotDate": 6, "holdings": 7}
    payload[6] = "2026-09-18"
    payload[7] = [8, 12]
    payload[8] = {"code": 9, "name": 10, "weight": 11, "shares": 11}
    payload[9] = "2330"
    payload[10] = "台積電"
    payload[11] = 9.63
    payload[12] = {"code": 13, "name": 14, "weight": 11, "shares": 11}
    payload[13] = "2454"
    payload[14] = "聯發科"
    payload[15] = {"code": 16, "name": 17, "shares": 18}
    payload[16] = "C_NTD"
    payload[17] = "CASH"
    payload[18] = 944387330
    import json
    html = (
        '<html><head><title>00980A</title></head><body>'
        '<p>公開持股明細資料，來源頁面內容足以進行保存與驗證。'
        '公開持股明細資料，來源頁面內容足以進行保存與驗證。'
        'public holdings data for deterministic parser validation. '
        'public holdings data for deterministic parser validation.</p>'
        '<script type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + '</script></body></html>'
    ).encode("utf-8")

    class Response:
        content = html
        text = html.decode("utf-8")
        headers = {"content-type": "text/html; charset=utf-8"}
        apparent_encoding = "utf-8"
        encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

    class Session:
        def get(self, _url: str, *, timeout: int):
            assert timeout == 30
            return Response()

    class Client:
        enabled = False

    result = fetch_and_save_holdings(
        ActiveEtf(
            symbol="00980A",
            name="Test ETF",
            issuer="Test Issuer",
            holdings_url="https://www.etfinfo.tw/etf/00980A/holdings",
        ),
        Client(),
        snapshot_date=dt.date(2026, 9, 19),
        root=tmp_path,
        session=Session(),
    )
    assert result.success is True
    assert result.holdings_count == 2
    assert result.snapshot_date == dt.date(2026, 9, 18)
    assert result.prompt_id == "etfinfo_holdings_parser"


def test_etfinfo_nuxt_payload_excludes_explicit_derivative_rows(
    tmp_path: Path,
) -> None:
    payload = [None] * 18
    payload[1] = {"data": 2}
    payload[2] = ["ShallowReactive", 3]
    payload[3] = {"etf-detail-base-00406A": 4}
    payload[4] = {"holdings": 5}
    payload[5] = {"snapshotDate": 6, "holdings": 7}
    payload[6] = "2026-09-18"
    payload[7] = [8, 12]
    payload[8] = {
        "code": 9, "name": 10, "weight": 11, "shares": 11,
        "assetType": 16,
    }
    payload[9] = "2330"
    payload[10] = "台積電"
    payload[11] = 9.63
    payload[12] = {
        "code": 13, "name": 14, "weight": 15, "shares": 15,
        "assetType": 17,
    }
    payload[13] = "TXY:202609:C"
    payload[14] = "台指選擇權"
    payload[15] = -5.83
    payload[16] = "stock"
    payload[17] = "option"
    import json
    html = (
        '<script type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + '</script>'
    )

    parsed = _parse_etfinfo_holdings_html(html, symbol="00406A")

    assert parsed is not None
    assert len(parsed[1]) == 1
    assert parsed[1][0].ticker == "2330"

from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import patch

from bot.mops_research import (
    INGEST_RUN_STATUS,
    claim_snippet_supported,
    materialize_mops_detail_slice,
    normalize_claim_whitespace,
)
from bot.mops_scraper import MaterialDetailResult, MaterialInfo, ingest_material_detail
from bot.query_plan import QueryPlan
from bot.resource_adapters import MopsAdapter
from bot.source_registry import SourceRegistry
from bot.stock_db import StockDB


class _Response:
    def __init__(self, content: bytes, *, status_code: int = 200):
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status_code
        self.encoding = "utf-8"


class _DetailSession:
    def __init__(self, html: bytes):
        self.html = html

    def get(self, _url, **_kwargs):
        return _Response(self.html)


def _seed_source(tmp_path: Path, text: str = "2330 資本支出維持高檔") -> str:
    doc = SourceRegistry(tmp_path).capture_document(
        "mops_material_detail",
        f"<html><body>{text}</html>",
        source_url="https://example.test/mops/detail/2330",
        source_ref="https://example.test/mops/detail/2330",
        title="MOPS 重大訊息詳情",
        suffix="html",
        mime_type="text/html",
    )
    return doc.document_id


def test_mops_detail_vertical_slice_links_raw_research_evidence_claim(tmp_path: Path) -> None:
    html = (
        "<html><body><p>2330 台積電公告：資本支出維持高檔，"
        "先進製程需求強勁。</p></body></html>"
    ).encode("utf-8")
    result = ingest_material_detail(
        "https://example.test/mops/detail/2330",
        session=_DetailSession(html),
        root=tmp_path,
        ticker="2330",
        materialize_research=True,
    )
    assert "資本支出" in result.text
    assert result.research_status in {"ok", "reused"}
    assert not result.research_error
    assert result.source_document_id
    assert result.research_document_id
    assert result.evidence_id
    assert result.claim_id
    assert result.run_id

    db = StockDB.open(root=tmp_path)
    source = db.get_source_document(result.source_document_id)
    assert source is not None
    assert source.source == "mops_material_detail"
    assert (tmp_path / source.content_path).read_bytes() == html

    research = db.get_research_document(result.research_document_id)
    assert research is not None
    assert research.source_document_id == result.source_document_id
    assert "資本支出" in research.canonical_text
    chunks = db.list_research_chunks(research.document_id)
    assert chunks
    assert "".join(chunk.text for chunk in chunks) == research.canonical_text

    evidence = db.get_evidence(result.evidence_id)
    assert evidence is not None
    assert evidence.source_document_id == result.source_document_id
    meta = evidence.metadata_json
    assert "exact" in meta or "mops_official_vertical_slice" in meta

    claim = db.get_claim(result.claim_id)
    assert claim is not None
    assert claim.run_id == result.run_id
    assert claim.status == "verified"
    linked = db.list_claim_evidence(result.claim_id)
    assert any(row.evidence_id == result.evidence_id for row in linked)


def test_mops_slice_rerun_is_idempotent(tmp_path: Path) -> None:
    source_id = _seed_source(tmp_path)
    text = "2330 資本支出維持高檔，先進製程需求強勁。"
    first = materialize_mops_detail_slice(
        root=tmp_path,
        source_document_id=source_id,
        canonical_text=text,
        detail_url="https://example.test/mops/detail/2330",
        ticker="2330",
    )
    second = materialize_mops_detail_slice(
        root=tmp_path,
        source_document_id=source_id,
        canonical_text=text,
        detail_url="https://example.test/mops/detail/2330",
        ticker="2330",
    )
    assert first.status == "ok"
    assert second.status == "reused"
    assert second.error == ""
    assert second.source_document_id == first.source_document_id == source_id
    assert second.research_document_id == first.research_document_id
    assert second.evidence_id == first.evidence_id
    assert second.claim_id == first.claim_id
    assert second.run_id == first.run_id

    db = StockDB.open(root=tmp_path)
    try:
        evidence_rows = [
            row for row in db.list_evidence(limit=50)
            if row.source_document_id == source_id
        ]
        assert len(evidence_rows) == 1
        assert db.get_claim(first.claim_id) is not None
        assert db.get_llm_run(first.run_id) is not None
    finally:
        db.close()


def test_claim_verification_tolerates_newline_tab_whitespace() -> None:
    raw = "資本支出\t維持高檔\n先進製程   需求強勁"
    assert normalize_claim_whitespace(raw) == "資本支出 維持高檔 先進製程 需求強勁"
    assert claim_snippet_supported("資本支出 維持高檔", raw)
    assert claim_snippet_supported("資本支出\n維持高檔", raw)
    assert claim_snippet_supported("資本支出\t維持高檔", "資本支出 維持高檔")


def test_mops_slice_claim_verified_with_messy_whitespace(tmp_path: Path) -> None:
    messy = "2330\t台積電公告：\n資本支出   維持高檔"
    source_id = _seed_source(tmp_path, messy)
    slice_row = materialize_mops_detail_slice(
        root=tmp_path,
        source_document_id=source_id,
        canonical_text=messy,
        detail_url="https://example.test/mops/detail/ws",
        ticker="2330",
    )
    assert slice_row.status == "ok"
    db = StockDB.open(root=tmp_path)
    try:
        claim = db.get_claim(slice_row.claim_id)
        assert claim is not None
        assert claim.status == "verified"
        assert "資本支出" in claim.text
    finally:
        db.close()


def test_ingest_run_status_differs_from_ask_verified(tmp_path: Path) -> None:
    source_id = _seed_source(tmp_path)
    slice_row = materialize_mops_detail_slice(
        root=tmp_path,
        source_document_id=source_id,
        canonical_text="2330 資本支出維持高檔",
        detail_url="https://example.test/mops/detail/status",
        ticker="2330",
    )
    assert slice_row.status == "ok"
    assert INGEST_RUN_STATUS == "source_ingested"
    assert INGEST_RUN_STATUS != "verified"
    db = StockDB.open(root=tmp_path)
    try:
        run = db.get_llm_run(slice_row.run_id)
        assert run is not None
        assert run.status == "source_ingested"
        assert run.status != "verified"
        assert run.task_type == "mops_material_detail_ingest"
        assert "ingest" in run.metadata_json
    finally:
        db.close()


def test_mops_slice_unknown_source_returns_failed_status(tmp_path: Path) -> None:
    slice_row = materialize_mops_detail_slice(
        root=tmp_path,
        source_document_id="source-doc:not-real",
        canonical_text="不可臆造來源",
    )
    assert slice_row.status == "failed"
    assert "unknown source_document_id" in slice_row.error
    assert slice_row.research_document_id == ""
    assert slice_row.evidence_id == ""
    assert slice_row.claim_id == ""


def test_mops_slice_does_not_guess_source_by_url(tmp_path: Path) -> None:
    SourceRegistry(tmp_path).capture_document(
        "mops_material_detail",
        "<html>真實原文 2330</html>",
        source_url="https://example.test/real",
        source_ref="https://example.test/real",
        title="real",
        suffix="html",
        mime_type="text/html",
    )
    # 即使 URL 相同，也不能用 URL 字串冒充 exact document id。
    slice_row = materialize_mops_detail_slice(
        root=tmp_path,
        source_document_id="https://example.test/real",
        canonical_text="真實原文 2330",
        detail_url="https://example.test/real",
    )
    assert slice_row.status == "failed"
    assert "unknown source_document_id" in slice_row.error


def test_ingest_material_detail_surfaces_empty_text_failure(tmp_path: Path) -> None:
    result = materialize_mops_detail_slice(
        root=tmp_path,
        source_document_id=_seed_source(tmp_path),
        canonical_text="   \n\t  ",
    )
    assert result.status == "failed"
    assert "不可為空" in result.error
    assert result.evidence_id == ""
    assert result.claim_id == ""


def test_mops_adapter_reuses_research_evidence_without_duplicate(
    tmp_path: Path,
) -> None:
    html = (
        "<html><body><p>2330 台積電公告：資本支出維持高檔。</p></body></html>"
    ).encode("utf-8")
    detail_url = "https://example.test/mops/detail/adapter"
    rows = [
        MaterialInfo(
            date=dt.date(2026, 9, 17),
            time="15:00",
            ticker="2330",
            company="台積電",
            subject="資本支出公告",
            detail_url=detail_url,
        ),
    ]
    plan = QueryPlan(
        plan_id="plan-mops-b1",
        trace_id="trace-mops-b1",
        query="2330 重大訊息",
        normalized_query="2330 重大訊息",
        ticker="2330",
        max_sources=3,
    )
    adapter = MopsAdapter(root=tmp_path, max_details=3)
    with patch("bot.mops_scraper.fetch_material_info", return_value=rows):
        with patch(
            "bot.mops_scraper._new_session",
            return_value=_DetailSession(html),
        ):
            result = adapter.collect(plan)

    assert result.status in {"ok", "partial"}
    assert len(result.evidence) == 1
    item = result.evidence[0]
    assert item["source_document_id"]
    assert item.get("provenance_status") == "exact"
    assert item["kind"] == "mops_material"
    assert item["metadata"].get("pipeline") == "mops_official_vertical_slice"

    db = StockDB.open(root=tmp_path)
    try:
        linked = [
            row for row in db.list_evidence(limit=50)
            if row.source_document_id == item["source_document_id"]
        ]
        assert len(linked) == 1
        assert linked[0].evidence_id == item["id"]
        assert linked[0].kind == "mops_material"
    finally:
        db.close()


def test_mops_adapter_non_replayable_source_does_not_reuse_stale_exact(
    tmp_path: Path,
) -> None:
    """清除不可回放來源後，不得把 DB 舊 evidence 預設成 exact 重用。"""
    source_id = _seed_source(tmp_path, "2330 資本支出維持高檔")
    slice_row = materialize_mops_detail_slice(
        root=tmp_path,
        source_document_id=source_id,
        canonical_text="2330 資本支出維持高檔",
        detail_url="https://example.test/mops/detail/stale",
        ticker="2330",
    )
    assert slice_row.status == "ok"
    assert slice_row.evidence_id
    assert slice_row.provenance_status == "exact"

    db = StockDB.open(root=tmp_path)
    try:
        source = db.get_source_document(source_id)
        assert source is not None
        raw_path = tmp_path / source.content_path
    finally:
        db.close()
    raw_path.write_bytes(b"<html><body>tampered</body></html>")

    detail_url = "https://example.test/mops/detail/stale"
    rows = [
        MaterialInfo(
            date=dt.date(2026, 9, 17),
            time="15:00",
            ticker="2330",
            company="台積電",
            subject="資本支出公告",
            detail_url=detail_url,
        ),
    ]
    plan = QueryPlan(
        plan_id="plan-mops-stale",
        trace_id="trace-mops-stale",
        query="2330 重大訊息",
        normalized_query="2330 重大訊息",
        ticker="2330",
        max_sources=3,
    )

    def fake_detail(*_args, **_kwargs):
        return MaterialDetailResult(
            text="2330 資本支出維持高檔",
            source_document_id=source_id,
            research_document_id=slice_row.research_document_id,
            evidence_id=slice_row.evidence_id,
            claim_id=slice_row.claim_id,
            run_id=slice_row.run_id,
            research_status="reused",
        )

    with patch("bot.mops_scraper.fetch_material_info", return_value=rows), patch(
        "bot.mops_scraper.ingest_material_detail", side_effect=fake_detail
    ):
        result = MopsAdapter(root=tmp_path, max_details=1).collect(plan)

    assert result.status in {"ok", "partial"}
    assert len(result.evidence) == 1
    packet = result.evidence[0]
    assert packet.get("provenance_status") == "unresolved"
    assert packet.get("provenance_status") != "exact"
    assert not packet.get("source_document_id")
    assert packet.get("id") != slice_row.evidence_id
    assert any("無法回放" in w for w in result.warnings)


def test_mops_adapter_propagates_live_budget_to_http_calls(tmp_path: Path) -> None:
    rows = [
        MaterialInfo(
            date=dt.date(2026, 9, 17),
            time="15:00",
            ticker="2330",
            company="台積電",
            subject="資本支出公告",
            detail_url="https://example.test/mops/detail/budget",
        ),
    ]
    plan = QueryPlan(
        plan_id="plan-mops-budget",
        trace_id="trace-mops-budget",
        query="2330 重大訊息",
        normalized_query="2330 重大訊息",
        ticker="2330",
        max_sources=3,
    )
    observed: dict[str, object] = {}

    def fake_fetch(*_args, **kwargs):
        observed["list_timeout"] = kwargs["timeout"]
        observed["deadline"] = kwargs["deadline"]
        return rows

    def fake_detail(*_args, **kwargs):
        observed["detail_timeout"] = kwargs["timeout"]
        return MaterialDetailResult(
            text="資本支出公告原文",
            research_status="skipped",
            research_error="test fixture",
        )

    with patch("bot.mops_scraper.fetch_material_info", side_effect=fake_fetch), \
            patch("bot.mops_scraper.ingest_material_detail", side_effect=fake_detail):
        result = MopsAdapter(
            root=tmp_path,
            max_details=1,
            timeout=0.5,
        ).collect(plan)

    assert result.status == "ok"
    assert 0.25 <= float(observed["list_timeout"]) <= 0.5
    assert 0.25 <= float(observed["detail_timeout"]) <= 0.5
    assert observed["deadline"]


def test_existing_fetch_material_detail_still_returns_text(tmp_path: Path) -> None:
    from bot.mops_scraper import fetch_material_detail

    html = "<html><body>法說會重點：毛利率展望穩健。</body></html>".encode("utf-8")
    text = fetch_material_detail(
        "https://example.test/detail2",
        session=_DetailSession(html),
        root=tmp_path,
    )
    assert "毛利率" in text
    db = StockDB.open(root=tmp_path)
    assert db.list_research_documents(limit=10)
    assert db.list_source_documents(source="mops_material_detail")

from __future__ import annotations

import json
from pathlib import Path

from bot.intraday_pipeline import load_intraday_by_date
from bot.next_day_watch_pipeline import load_next_day_by_asof, load_next_day_by_date
from bot.pipeline_shared import repair_stored_report_provenance
from bot.stock_db import LlmDailyReportRow, StockDB


def test_load_intraday_by_date_reads_db(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="intraday",
        report_date="2026-06-02",
        asof="2026-06-02",
        market_tone="bullish",
        brief_md="brief",
        payload_json='{"asof":"2026-06-02","market_tone":"bullish"}',
    ))

    data = load_intraday_by_date(tmp_path, "2026-06-02")

    assert data is not None
    assert data["asof"] == "2026-06-02"
    assert data["market_tone"] == "bullish"
    assert data["brief_md"] == "brief"


def test_load_next_day_by_asof_prefers_update(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="next_day_watch",
        report_date="2026-06-03",
        mode="draft",
        asof="2026-06-02",
        payload_json='{"asof":"2026-06-02","target_date":"2026-06-03","mode":"draft"}',
    ))
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="next_day_watch",
        report_date="2026-06-03",
        mode="update",
        asof="2026-06-02",
        payload_json='{"asof":"2026-06-02","target_date":"2026-06-03","mode":"update"}',
    ))

    data = load_next_day_by_asof(tmp_path, "2026-06-02")
    draft = load_next_day_by_asof(tmp_path, "2026-06-02", mode="draft")

    assert data is not None
    assert data["mode"] == "update"
    assert draft is not None
    assert draft["mode"] == "draft"


def test_load_next_day_by_date_reads_target_date(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="next_day_watch",
        report_date="2026-06-03",
        mode="draft",
        asof="2026-06-02",
        payload_json='{"asof":"2026-06-02","target_date":"2026-06-03","mode":"draft"}',
    ))

    data = load_next_day_by_date(tmp_path, "2026-06-03")

    assert data is not None
    assert data["target_date"] == "2026-06-03"


def test_report_file_replay_does_not_restore_legacy_evidence_as_exact(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "data" / "intraday" / "2026-06-02" / "report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(json.dumps({
        "asof": "2026-06-02",
        "evidence": [{
            "id": "news:legacy-1",
            "kind": "news",
            "source": "cnyes",
            "source_ref": "legacy-1",
            "title": "舊版新聞",
            "excerpt": "舊版摘要",
        }],
    }, ensure_ascii=False), encoding="utf-8")

    data = load_intraday_by_date(tmp_path, "2026-06-02")

    assert data is not None
    assert data["provenance_status"] == "unresolved"
    assert data["evidence"][0]["source_document_id"] == ""
    assert data["evidence"][0]["provenance_status"] == "unresolved"
    db = StockDB.open(root=tmp_path)
    row = db.get_llm_daily_report("intraday", "2026-06-02", mode="")
    assert row is not None
    assert row.quality_status == "partial"
    assert json.loads(row.evidence_json)[0]["source_document_id"] == ""


def test_repair_stored_report_provenance_is_idempotent(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    db.upsert_llm_daily_report(LlmDailyReportRow(
        report_type="next_day_watch",
        report_date="2026-06-03",
        mode="draft",
        asof="2026-06-02",
        payload_json=json.dumps({
            "target_date": "2026-06-03",
            "evidence": [{"id": "news:legacy", "title": "legacy"}],
        }),
        evidence_json=json.dumps([{"id": "news:legacy", "title": "legacy"}]),
        quality_status="ok",
    ))

    first = repair_stored_report_provenance(tmp_path)
    second = repair_stored_report_provenance(tmp_path)

    assert first["changed"] == 1
    assert second["changed"] == 0
    repaired = db.get_llm_daily_report("next_day_watch", "2026-06-03", mode="draft")
    assert repaired is not None
    assert repaired.quality_status == "partial"
    assert json.loads(repaired.payload_json)["provenance_status"] == "unresolved"

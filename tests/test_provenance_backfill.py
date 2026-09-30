"""exact-only provenance backfill 契約測試。

覆蓋 JSONL known/unknown evidence、dry-run 不寫入、apply 只鏈已知 ID、
缺證據不可發布、legacy analysis 明確 unresolved、重跑冪等，以及 CLI JSON／--limit。
"""

from __future__ import annotations

import json
from pathlib import Path

from bot.provenance_backfill import backfill_llm_provenance
from bot.provenance_backfill_cli import main as provenance_backfill_main
from bot.stock_db import EvidenceItemRow, LlmAnalysisRow, StockDB


def _write_jsonl(root: Path, rows: list[dict]) -> None:
    log_dir = root / "log" / "llm_calls"
    log_dir.mkdir(parents=True)
    path = log_dir / "llm_calls_2026-09-22.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _jsonl_record(
    *,
    run_id: str,
    output: dict | str,
    evidence_ids: list[str] | None = None,
    success: bool = True,
) -> dict:
    metadata: dict = {"task": "fixture", "run_id": run_id, "trace_id": f"trace:{run_id}"}
    if evidence_ids is not None:
        metadata["evidence_ids"] = list(evidence_ids)
    return {
        "ts": "2026-09-22T10:00:00+08:00",
        "prompt_id": "fixture",
        "prompt_version": "1.0",
        "model": "test-model",
        "input": "fixture input",
        "output": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
        "latency_ms": 1,
        "success": success,
        "error": "",
        "metadata": metadata,
    }


def _output_publishable(db: StockDB, run_id: str) -> int:
    row = db.conn.execute(
        "SELECT is_publishable FROM llm_outputs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert row is not None
    return int(row["is_publishable"])


def test_dry_run_counts_known_and_unknown_without_mutating(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_evidence(EvidenceItemRow(
            evidence_id="ev:known",
            source="fixture",
            excerpt="已知證據",
            quality_status="ok",
        ))
        _write_jsonl(tmp_path, [
            _jsonl_record(
                run_id="run:known",
                evidence_ids=["ev:known"],
                output={
                    "answer": "有證據",
                    "confidence": 0.8,
                    "evidence_ids": ["ev:known"],
                },
            ),
            _jsonl_record(
                run_id="run:unknown",
                evidence_ids=["ev:missing"],
                output={
                    "answer": "缺證據",
                    "confidence": 0.2,
                    "evidence_ids": ["ev:missing"],
                },
            ),
        ])

        report = backfill_llm_provenance(root=tmp_path, db=db, apply=False)

        assert report["dry_run"] is True
        assert report["records"] == 2
        assert report["unresolved_evidence"] == 1
        assert report["unresolved_records"] == 1
        assert report["runs_upserted"] == 0
        assert report["outputs_upserted"] == 0
        assert report["evidence_links"] == 0
        assert db.get_llm_run("run:known") is None
        assert db.get_llm_run("run:unknown") is None
        assert db.list_llm_run_evidence("run:known") == []
    finally:
        db.close()


def test_apply_links_only_known_exact_ids_and_blocks_publish_when_missing(
    tmp_path: Path,
) -> None:
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_evidence(EvidenceItemRow(
            evidence_id="ev:known",
            source="fixture",
            excerpt="已知證據",
            quality_status="ok",
        ))
        _write_jsonl(tmp_path, [
            _jsonl_record(
                run_id="run:exact",
                evidence_ids=["ev:known"],
                output={
                    "answer": "可發布",
                    "confidence": 0.9,
                    "evidence_ids": ["ev:known"],
                    "claims": [{
                        "text": "已知事實",
                        "evidence_ids": ["ev:known"],
                        "confidence": 0.9,
                    }],
                },
            ),
            _jsonl_record(
                run_id="run:mixed",
                evidence_ids=["ev:known", "ev:ghost"],
                output={
                    "answer": "不可發布",
                    "confidence": 0.5,
                    "evidence_ids": ["ev:known", "ev:ghost"],
                    "claims": [{
                        "text": "混雜引用",
                        "evidence_ids": ["ev:known", "ev:ghost"],
                    }],
                },
            ),
        ])

        report = backfill_llm_provenance(root=tmp_path, db=db, apply=True)

        assert report["dry_run"] is False
        assert report["records"] == 2
        assert report["runs_upserted"] == 2
        assert report["outputs_upserted"] == 2
        assert report["claims_upserted"] == 2
        assert report["evidence_links"] == 2  # only known IDs across both runs
        assert report["unresolved_evidence"] == 1
        assert report["unresolved_records"] == 1

        exact_ids = [item.evidence_id for item in db.list_llm_run_evidence("run:exact")]
        mixed_ids = [item.evidence_id for item in db.list_llm_run_evidence("run:mixed")]
        assert exact_ids == ["ev:known"]
        assert mixed_ids == ["ev:known"]
        assert "ev:ghost" not in mixed_ids
        assert db.get_evidence("ev:ghost") is None

        assert _output_publishable(db, "run:exact") == 1
        assert _output_publishable(db, "run:mixed") == 0
        assert db.get_claim("run:exact:claim:0").status == "verified"
        assert db.get_claim("run:mixed:claim:0").status == "unverified"
    finally:
        db.close()


def test_legacy_llm_analysis_history_stays_explicitly_unresolved(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_llm_analysis(LlmAnalysisRow(
            id="legacy-no-evidence",
            ts="2026-09-17T10:00:00+08:00",
            prompt_id="legacy",
            model="old-model",
            raw_output='{"answer":"舊分析"}',
            summary="舊分析",
            evidence_json="[]",
        ))
        db.upsert_llm_analysis(LlmAnalysisRow(
            id="legacy-missing-evidence",
            ts="2026-09-17T10:01:00+08:00",
            prompt_id="legacy",
            model="old-model",
            raw_output='{"answer":"缺證舊分析"}',
            summary="缺證舊分析",
            evidence_json='["ev:never-existed"]',
        ))

        report = backfill_llm_provenance(root=tmp_path, db=db, apply=True)

        assert report["records"] == 0
        assert report["legacy_records"] == 2
        assert report["legacy_unresolved"] == 2
        assert report["legacy_runs_upserted"] == 2

        for analysis_id in ("legacy-no-evidence", "legacy-missing-evidence"):
            run = db.get_llm_run(f"legacy-analysis:{analysis_id}")
            assert run is not None
            assert run.status == "legacy_unresolved"
            assert "no provable evidence" in run.error
            assert db.list_llm_run_evidence(run.run_id) == []
            assert _output_publishable(db, run.run_id) == 0
        assert db.get_evidence("ev:never-existed") is None
    finally:
        db.close()


def test_jsonl_without_evidence_is_not_publishable(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    try:
        _write_jsonl(tmp_path, [_jsonl_record(
            run_id="run:no-evidence",
            output={"answer": "沒有來源的答案", "claims": []},
        )])
        report = backfill_llm_provenance(root=tmp_path, db=db, apply=True)
        assert report["outputs_upserted"] == 1
        row = db.conn.execute(
            "SELECT is_publishable FROM llm_outputs WHERE run_id = ?",
            ("run:no-evidence",),
        ).fetchone()
        assert row is not None
        assert int(row["is_publishable"]) == 0
    finally:
        db.close()


def test_apply_rerun_is_idempotent_for_links_and_counts(tmp_path: Path) -> None:
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_evidence(EvidenceItemRow(
            evidence_id="ev:known",
            source="fixture",
            excerpt="已知證據",
            quality_status="ok",
        ))
        _write_jsonl(tmp_path, [
            _jsonl_record(
                run_id="run:idempotent",
                evidence_ids=["ev:known"],
                output={
                    "answer": "重跑",
                    "confidence": 0.7,
                    "evidence_ids": ["ev:known"],
                },
            ),
        ])

        first = backfill_llm_provenance(root=tmp_path, db=db, apply=True)
        second = backfill_llm_provenance(root=tmp_path, db=db, apply=True)

        assert first["records"] == second["records"] == 1
        assert first["runs_upserted"] == second["runs_upserted"] == 1
        assert first["outputs_upserted"] == second["outputs_upserted"] == 1
        assert first["evidence_links"] == second["evidence_links"] == 1
        assert first["unresolved_evidence"] == second["unresolved_evidence"] == 0
        linked = db.list_llm_run_evidence("run:idempotent")
        assert [item.evidence_id for item in linked] == ["ev:known"]
        link_rows = db.conn.execute(
            "SELECT COUNT(*) AS n FROM llm_run_evidence WHERE run_id = ?",
            ("run:idempotent",),
        ).fetchone()
        assert int(link_rows["n"]) == 1
    finally:
        db.close()


def test_cli_json_output_and_limit(monkeypatch, tmp_path: Path, capsys) -> None:
    calls: dict = {}

    def fake_backfill(**kwargs):
        calls.update(kwargs)
        return {
            "dry_run": not kwargs["apply"],
            "records": 0,
            "runs_upserted": 0,
            "outputs_upserted": 0,
            "claims_upserted": 0,
            "evidence_links": 0,
            "unresolved_evidence": 0,
            "unresolved_records": 0,
            "legacy_records": 0,
            "legacy_unresolved": 0,
            "legacy_runs_upserted": 0,
        }

    monkeypatch.setattr(
        "bot.provenance_backfill_cli.backfill_llm_provenance",
        fake_backfill,
    )

    assert provenance_backfill_main([
        "--root", str(tmp_path), "--limit", "7",
    ]) == 0
    assert calls["root"] == tmp_path
    assert calls["limit"] == 7
    assert calls["apply"] is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True

    assert provenance_backfill_main([
        "--root", str(tmp_path), "--apply", "--limit", "3",
    ]) == 0
    assert calls["apply"] is True
    assert calls["limit"] == 3
    assert json.loads(capsys.readouterr().out)["dry_run"] is False


def test_cli_limit_caps_jsonl_iteration(tmp_path: Path, capsys) -> None:
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_evidence(EvidenceItemRow(
            evidence_id="ev:known",
            source="fixture",
            excerpt="已知證據",
            quality_status="ok",
        ))
        _write_jsonl(tmp_path, [
            _jsonl_record(
                run_id=f"run:limit-{index}",
                evidence_ids=["ev:known"],
                output={"answer": f"r{index}", "confidence": 0.5, "evidence_ids": ["ev:known"]},
            )
            for index in range(3)
        ])
    finally:
        db.close()

    assert provenance_backfill_main([
        "--root", str(tmp_path), "--limit", "1",
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["records"] == 1
    assert report["runs_upserted"] == 0

"""本地整合前置檢查：只讀、可追溯、可選服務明確分離。"""

from __future__ import annotations

from types import SimpleNamespace

import bot.system_preflight as module
from bot.memory_store import MemoryStore
from bot.stock_db import EvidenceItemRow, StockDB


def _audit(*, gaps=None, orphans=None):
    return {
        "database": {
            "exists": True,
            "integrity": {
                "sqlite": {"status": "ok", "details": ["ok"]},
                "orphan_counts": orphans or {},
                "fts": {
                    "research_chunks": {"consistent": True},
                    "memory_items": {"consistent": True},
                },
            },
        },
        "raw_archive": {"checked": 3, "missing": [], "mismatched": []},
        "gaps": gaps or [],
    }


def _settings(**overrides):
    values = {
        "gemini_api_key": "",
        "gemini_model": "gemini-2.5-flash",
        "google_sheet_id": "",
        "google_sa_json_path": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_local_core_is_ready_without_optional_services(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "audit_project", lambda _root: _audit())

    report = module.run_system_preflight(
        tmp_path,
        settings=_settings(),
        check_imports=False,
    )

    assert report.status == "ready"
    assert not report.has_fail()
    assert any(item.name == "SQLite 完整性" and item.status == "ok" for item in report.checks)
    assert any(item.name == "Gemini LLM 設定" and item.status == "info" for item in report.checks)
    assert any(item.name == "Google Sheets 同步設定" and item.status == "info" for item in report.checks)


def test_audit_error_blocks_integration(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "audit_project", lambda _root: _audit(
        orphans={"claim_evidence": 1},
        gaps=[{"code": "relational_orphan_links", "severity": "error"}],
    ))

    report = module.run_system_preflight(
        tmp_path,
        settings=_settings(),
        check_imports=False,
    )

    assert report.status == "blocked"
    provenance = next(item for item in report.checks if item.name == "provenance 關聯")
    assert provenance.status == "fail"


def test_partial_cloud_configuration_is_warning_without_secret_echo(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "audit_project", lambda _root: _audit())
    secret_path = tmp_path / "missing-service-account.json"

    report = module.run_system_preflight(
        tmp_path,
        settings=_settings(
            google_sheet_id="sheet-id-that-must-not-be-echoed",
            google_sa_json_path=str(secret_path),
        ),
        check_imports=False,
    )

    check = next(item for item in report.checks if item.name == "Google Sheets 同步設定")
    assert check.status == "warn"
    assert "sheet-id-that-must-not-be-echoed" not in check.detail
    assert "missing-service-account.json" in check.detail


def test_memory_preflight_accepts_exact_evidence_and_reports_capacity(tmp_path):
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_evidence(EvidenceItemRow(
            evidence_id="ev:preflight",
            kind="official",
            source="fixture",
            excerpt="可回放證據",
            quality_status="ok",
        ))
        MemoryStore(db).put_short(
            "有 exact evidence 的記憶",
            memory_id="memory:preflight",
            quality_status="ok",
            evidence=["ev:preflight"],
        )
    finally:
        db.close()

    checks = module._memory_checks(tmp_path)
    evidence_check = next(item for item in checks if item.name == "記憶 evidence 與容量")
    capacity_check = next(item for item in checks if item.name == "記憶容量")
    assert evidence_check.status == "ok"
    assert capacity_check.status == "ok"
    assert evidence_check.extra["active_short"] == 1


def test_memory_preflight_blocks_missing_evidence_reference(tmp_path):
    db = StockDB.open(root=tmp_path)
    try:
        MemoryStore(db).put_short(
            "沒有登錄證據的記憶",
            memory_id="memory:missing-evidence",
            quality_status="ok",
            evidence=["ev:not-registered"],
        )
    finally:
        db.close()

    check = next(
        item for item in module._memory_checks(tmp_path)
        if item.name == "記憶 evidence 與容量"
    )
    assert check.status == "fail"
    assert "memory:missing-evidence" in check.extra["invalid_memory_ids"]


def test_memory_preflight_blocks_misaligned_memory_evidence(tmp_path):
    db = StockDB.open(root=tmp_path)
    try:
        db.upsert_evidence(EvidenceItemRow(
            evidence_id="ev:aligned",
            kind="official",
            source="fixture",
            excerpt="登錄但未連結",
            quality_status="ok",
        ))
        MemoryStore(db).put_short(
            "evidence_json 與 memory_evidence 未對齊",
            memory_id="memory:misaligned",
            quality_status="ok",
            evidence=["ev:aligned"],
        )
        db.conn.execute(
            "DELETE FROM memory_evidence WHERE memory_id = ?",
            ("memory:misaligned",),
        )
        db.conn.commit()
    finally:
        db.close()

    check = next(
        item for item in module._memory_checks(tmp_path)
        if item.name == "記憶 evidence 與容量"
    )
    assert check.status == "fail"
    assert "memory:misaligned" in check.extra["misaligned_memory_ids"]
    assert check.extra["invalid_memory_ids"] == []


def test_memory_preflight_flags_long_research_empty_and_non_exact_evidence(tmp_path):
    from bot.stock_db import MemoryItemRow
    from bot.utils import now_tw

    db = StockDB.open(root=tmp_path)
    try:
        now = now_tw().isoformat(timespec="seconds")
        db.upsert_evidence(EvidenceItemRow(
            evidence_id="ev:preflight-unresolved",
            kind="news",
            source="legacy",
            excerpt="unresolved",
            quality_status="ok",
            metadata_json='{"provenance_status":"unresolved"}',
        ))
        db.upsert_memory_item(MemoryItemRow(
            memory_id="memory:long-empty",
            memory_layer="long",
            memory_type="research_summary",
            content="空 evidence 研究",
            summary="空 evidence",
            quality_status="ok",
            memory_status="active",
            created_at=now,
            observed_at=now,
            updated_at=now,
            evidence_json="[]",
        ))
        db.upsert_memory_item(MemoryItemRow(
            memory_id="memory:long-unresolved",
            memory_layer="long",
            memory_type="daily_report",
            content="非 exact 報告",
            summary="非 exact",
            quality_status="ok",
            memory_status="active",
            created_at=now,
            observed_at=now,
            updated_at=now,
            evidence_json='["ev:preflight-unresolved"]',
        ))
        db.link_memory_evidence("memory:long-unresolved", "ev:preflight-unresolved")
        MemoryStore(db).put_long(
            "偏好不需市場 evidence",
            memory_id="memory:long-preference",
            memory_type="preference",
            quality_status="ok",
        )
    finally:
        db.close()

    checks = module._memory_checks(tmp_path)
    provenance = next(item for item in checks if item.name == "長期研究記憶 provenance")
    assert provenance.status == "fail"
    assert "exact／composite_exact" in provenance.suggestion
    assert "memory:long-empty" in provenance.extra["empty_evidence_memory_ids"]
    assert "memory:long-unresolved" in provenance.extra["non_exact_memory_ids"]
    assert "memory:long-preference" not in provenance.extra["empty_evidence_memory_ids"]


def test_memory_preflight_accepts_exact_long_research_and_flags_non_replayable(tmp_path):
    from bot.source_registry import SourceRegistry

    db = StockDB.open(root=tmp_path)
    try:
        store = MemoryStore(db)
        document = SourceRegistry(tmp_path, db).register_document(
            "fixture", b"preflight-exact", suffix="txt",
        )
        db.upsert_evidence(EvidenceItemRow(
            evidence_id="ev:preflight-exact",
            kind="research",
            source="fixture",
            source_document_id=document.document_id,
            excerpt="preflight-exact",
            quality_status="ok",
            metadata_json='{"provenance_status":"exact"}',
        ))
        store.put_long(
            "可回放研究",
            memory_id="memory:long-exact",
            memory_type="research_summary",
            quality_status="ok",
            evidence=["ev:preflight-exact"],
        )
        ok_checks = module._memory_checks(tmp_path)
        ok = next(item for item in ok_checks if item.name == "長期研究記憶 provenance")
        assert ok.status == "ok"

        (tmp_path / document.content_path).write_bytes(b"tampered-preflight")
    finally:
        db.close()

    checks = module._memory_checks(tmp_path)
    provenance = next(item for item in checks if item.name == "長期研究記憶 provenance")
    assert provenance.status == "fail"
    assert "memory:long-exact" in provenance.extra["non_replayable_memory_ids"]
    assert "不得依 URL／標題／相似度猜補" in provenance.suggestion

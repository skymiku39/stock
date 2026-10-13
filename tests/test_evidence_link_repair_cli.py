from __future__ import annotations

import json

from bot.evidence_link_repair_cli import main


def test_evidence_link_repair_cli_defaults_to_dry_run(monkeypatch, tmp_path, capsys):
    calls = {}

    def fake_repair(**kwargs):
        calls.update(kwargs)
        return {"dry_run": not kwargs["apply"], "links_added": 0}

    monkeypatch.setattr(
        "bot.evidence_link_repair_cli.repair_evidence_source_links",
        fake_repair,
    )

    assert main(["--root", str(tmp_path), "--limit", "12"]) == 0
    assert calls["root"] == tmp_path.resolve()
    assert calls["limit"] == 12
    assert calls["apply"] is False
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_evidence_link_repair_cli_apply_is_explicit(monkeypatch, tmp_path, capsys):
    calls = {}

    def fake_repair(**kwargs):
        calls.update(kwargs)
        return {"dry_run": False, "links_added": 3}

    monkeypatch.setattr(
        "bot.evidence_link_repair_cli.repair_evidence_source_links",
        fake_repair,
    )

    assert main(["--root", str(tmp_path), "--apply"]) == 0
    assert calls["apply"] is True
    assert json.loads(capsys.readouterr().out)["links_added"] == 3

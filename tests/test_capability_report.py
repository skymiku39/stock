from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_AUDIT = ROOT / "tests" / "fixtures" / "capability_report" / "audit_sample.json"


def _load_report_generator():
    path = ROOT / "scripts" / "generate_capability_report.py"
    spec = importlib.util.spec_from_file_location("generate_capability_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_console_script_has_capability_metadata() -> None:
    module = _load_report_generator()
    scripts = module._load_toml_scripts(ROOT / "pyproject.toml")

    names = {name for name, _target in scripts}
    missing = sorted(names - set(module.CLI_META))

    assert not missing, f"新增 console script 缺少能力分類：{missing}"


def test_capability_report_counts_current_console_scripts() -> None:
    module = _load_report_generator()
    scripts = module._load_toml_scripts(ROOT / "pyproject.toml")

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert dict(scripts) == declared
    assert {name for name, _target in scripts} >= {
        "stock-system-preflight",
        "stock-evidence-link-repair",
        "stock-ask",
        "stock-test",
    }


def test_capability_report_preserves_page_and_prompt_inventory() -> None:
    module = _load_report_generator()
    pages, _groups = module._parse_nav_pages(ROOT / "src" / "bot" / "dashboard" / "nav.py")
    prompts = module._read_prompt_meta(ROOT / "prompts")

    assert len(pages) == 30
    assert len(prompts) == 15
    assert {p["id"] for p in prompts} >= {
        "query_plan",
        "ask_answer",
        "research_ticker",
    }


def test_missing_cli_metadata_fails_before_writing_output(tmp_path: Path) -> None:
    module = _load_report_generator()
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project.scripts]\n'
        'stock-bot = "bot.main:main"\n'
        'stock-untracked = "bot.untracked:main"\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "bot" / "dashboard").mkdir(parents=True)
    (tmp_path / "src" / "bot" / "dashboard" / "nav.py").write_text(
        'PAGES = {"總覽": object}\nNAV_GROUPS = {"監控": ["總覽"]}\n',
        encoding="utf-8",
    )
    (tmp_path / "prompts").mkdir()
    (tmp_path / "scripts").mkdir()

    out = tmp_path / "out" / "report.html"
    rc = module.main(["--root", str(tmp_path), "--out", str(out)])

    assert rc == 2
    assert not out.exists()


def test_generated_html_has_no_external_runtime_dependency(tmp_path: Path) -> None:
    module = _load_report_generator()
    out = tmp_path / "system_capability_report.html"
    rc = module.main(["--root", str(ROOT), "--out", str(out)])

    assert rc == 0
    assert out.is_file()
    html = out.read_text(encoding="utf-8")

    assert "cdn.jsdelivr.net" not in html
    assert "https://" not in html
    assert "http://" not in html
    assert "<script" not in html.lower()
    assert 'class="mermaid"' in html
    assert "flowchart" in html
    assert "未提供" in html
    assert "計數不可用" in html
    assert "4 筆 legacy source run" not in html
    assert "43 筆 legacy LLM" not in html


def test_derive_audit_gap_metrics_from_fixture() -> None:
    module = _load_report_generator()
    audit = module.load_audit_json(FIXTURE_AUDIT)
    metrics = module.derive_audit_gap_metrics(audit)

    assert metrics["supplied"] is True
    assert metrics["source_runs_without_raw_manifest"] == 2
    assert metrics["legacy_llm_without_provable_evidence"] == 5


def test_generated_html_uses_audit_json_counts(tmp_path: Path) -> None:
    module = _load_report_generator()
    out = tmp_path / "system_capability_report.html"
    rc = module.main([
        "--root", str(ROOT),
        "--out", str(out),
        "--audit-json", str(FIXTURE_AUDIT),
    ])

    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert "source_runs_without_raw_manifest" in html
    assert "legacy_llm_without_provable_evidence" in html
    assert "2 筆" in html
    assert "5 筆" in html
    assert "未提供" not in html
    assert "cdn.jsdelivr.net" not in html
    assert "<script" not in html.lower()


def test_invalid_audit_json_fails_before_writing(tmp_path: Path) -> None:
    module = _load_report_generator()
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    out = tmp_path / "out.html"
    rc = module.main([
        "--root", str(ROOT),
        "--out", str(out),
        "--audit-json", str(bad),
    ])
    assert rc == 2
    assert not out.exists()

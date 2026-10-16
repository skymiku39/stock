from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("flags,enabled", [(["--no-brief"], False), ([], True)])
def test_brief_switch_controls_both_daily_and_us_briefs(tmp_path, monkeypatch, flags, enabled):
    from bot import auto_research
    from bot.config import Settings
    settings = Settings(_env_file=None, gemini_api_key="fixture", llm_provider="gemini")
    monkeypatch.setattr(auto_research, "Settings", lambda **kwargs: settings)
    monkeypatch.setattr(auto_research, "get_or_create_bus", lambda **kwargs: None)
    captured = []
    def run(config, **kwargs):
        captured.append(config)
        return SimpleNamespace(run_id="fixture", errors=[], warnings=[], focus_tickers=[], output_dir=str(tmp_path))
    monkeypatch.setattr(auto_research, "run_full_pipeline", run)
    from bot import verified_research_batch
    verified_calls = []
    monkeypatch.setattr(verified_research_batch, "main", lambda args: verified_calls.append(args) or 0)
    assert auto_research.main(["--root", str(tmp_path), *flags]) == 0
    assert captured[0].generate_brief is enabled
    assert captured[0].generate_us_brief is enabled
    assert verified_calls == [["--root", str(tmp_path), "--source-run", "fixture", "--execute"]]

@pytest.mark.parametrize('flags,expected', [
    (['--no-verified-backlog'], None), (['--no-auto-research'], None),
    (['--no-llm'], None), (['--auto-research-limit','5'], '--selected-only'),
])
def test_verified_backlog_respects_opt_out_and_scheduler_budget(tmp_path, monkeypatch, flags, expected):
    from bot import auto_research, verified_research_batch
    from bot.config import Settings
    settings=Settings(_env_file=None, gemini_api_key='fixture', llm_provider='gemini')
    monkeypatch.setattr(auto_research,'Settings',lambda **kw:settings)
    monkeypatch.setattr(auto_research,'get_or_create_bus',lambda **kw:None)
    monkeypatch.setattr(auto_research,'run_full_pipeline',lambda *a,**kw:SimpleNamespace(
        run_id='fixture',errors=[],warnings=[],focus_tickers=[],output_dir=str(tmp_path)))
    calls=[]
    monkeypatch.setattr(verified_research_batch,'main',lambda args:calls.append(args) or 3)
    assert auto_research.main(['--root',str(tmp_path),*flags]) == (3 if expected else 0)
    assert (calls and calls[0][-1]==expected) if expected else not calls

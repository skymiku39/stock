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
    monkeypatch.setattr('time.sleep', lambda _: None)
    calls=[]
    monkeypatch.setattr(verified_research_batch,'main',lambda args:calls.append(args) or 3)
    assert auto_research.main(['--root',str(tmp_path),*flags]) == (3 if expected else 0)
    assert (calls and calls[0][-1]==expected) if expected else not calls


def test_pipeline_errors_do_not_suppress_verified_recovery_or_become_success(tmp_path, monkeypatch):
    from bot import auto_research, verified_research_batch
    from bot.config import Settings
    settings=Settings(_env_file=None, gemini_api_key='fixture', llm_provider='gemini')
    monkeypatch.setattr(auto_research,'Settings',lambda **kw:settings)
    monkeypatch.setattr(auto_research,'get_or_create_bus',lambda **kw:None)
    monkeypatch.setattr(auto_research,'run_full_pipeline',lambda *a,**kw:SimpleNamespace(
        run_id='fixture',errors=['upstream failure'],warnings=[],focus_tickers=[],output_dir=str(tmp_path)))
    monkeypatch.setattr('time.sleep', lambda _: None)
    calls=[]
    monkeypatch.setattr(verified_research_batch,'main',lambda args:calls.append(args) or 0)
    assert auto_research.main(['--root',str(tmp_path)])==2
    assert len(calls)==1


@pytest.mark.parametrize("outcomes, expected", [([3, 2, 0], 0), ([3, 3, 3], 3), ([RuntimeError("network"), 0], 0)])
def test_verified_retry_keeps_same_source_run(tmp_path, monkeypatch, outcomes, expected):
    from bot import auto_research, verified_research_batch
    from bot.config import Settings
    settings = Settings(_env_file=None, gemini_api_key="fixture", llm_provider="gemini")
    monkeypatch.setattr(auto_research, "Settings", lambda **kw: settings)
    monkeypatch.setattr(auto_research, "get_or_create_bus", lambda **kw: None)
    monkeypatch.setattr(auto_research, "run_full_pipeline", lambda *a, **kw: SimpleNamespace(
        run_id="same-run", errors=[], warnings=[], focus_tickers=[], output_dir=str(tmp_path)))
    calls, delays = [], []
    remaining = iter(outcomes)
    def retry(args):
        calls.append(list(args))
        result = next(remaining)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(verified_research_batch, "main", retry)
    monkeypatch.setattr("time.sleep", delays.append)
    assert auto_research.main(["--root", str(tmp_path), "--auto-research-limit", "12"]) == expected
    assert len(calls) == len(outcomes)
    assert all(args == calls[0] for args in calls)
    assert calls[0][-1] == "--selected-only"
    assert delays == [60, 120][:len(outcomes)-1]

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
    assert auto_research.main(["--root", str(tmp_path), *flags]) == 0
    assert captured[0].generate_brief is enabled
    assert captured[0].generate_us_brief is enabled

from types import SimpleNamespace
from unittest.mock import Mock

from bot.config import Settings
from bot.model_recovery import refresh_blocked_model
from bot.runtime_state import FileLock, atomic_json, read_json
from bot.scheduler import Job, Scheduler
from bot.utils import now_tw


def blocked(tmp_path, monkeypatch, health):
    settings = Settings(_env_file=None, llm_provider="codex_app_server")
    path = tmp_path / "data/llm_availability.json"
    atomic_json(path, {
        "configuration": f"{settings.codex_app_server_url}|{settings.codex_app_server_model}|{settings.codex_app_server_effort}",
        "status": "blocked", "checked_at": 100, "next_retry_at": 100000,
        "reason": "model quota unavailable", "error_kind": "quota",
    })
    client = SimpleNamespace(health=Mock(return_value=health))
    monkeypatch.setattr("bot.model_recovery.CodexAppServerClient", lambda **kwargs: client)
    return settings, path, client


def test_restored_access_reopens_gate_before_old_quota_reset(tmp_path, monkeypatch):
    settings, path, client = blocked(tmp_path, monkeypatch, {"available": True})
    assert refresh_blocked_model(tmp_path, settings, now=500) == 500
    state = read_json(path)
    assert state["status"] == "recovered"
    assert state["next_retry_at"] == 0
    assert state["recovered_at"] == 500
    assert refresh_blocked_model(tmp_path, settings, now=510) == 500
    client.health.assert_called_once_with(timeout=10)


def test_unavailable_probe_is_throttled_and_preserves_block(tmp_path, monkeypatch):
    settings, path, client = blocked(tmp_path, monkeypatch, {"available": False})
    assert refresh_blocked_model(tmp_path, settings, now=200) == 0
    client.health.assert_not_called()
    assert refresh_blocked_model(tmp_path, settings, now=500) == 0
    assert refresh_blocked_model(tmp_path, settings, now=600) == 0
    client.health.assert_called_once()
    assert read_json(path)["next_retry_at"] == 100000
    assert read_json(path)["status"] == "blocked"


def test_active_model_call_prevents_recovery_probe(tmp_path, monkeypatch):
    settings, path, client = blocked(tmp_path, monkeypatch, {"available": True})
    before = path.read_bytes()
    with FileLock(tmp_path / "data/.llm-call.lock"):
        assert refresh_blocked_model(tmp_path, settings, now=500) == 0
    client.health.assert_not_called()
    assert path.read_bytes() == before


def test_research_resumes_once_and_then_respects_normal_interval(tmp_path, monkeypatch):
    settings, path, client = blocked(tmp_path, monkeypatch, {"available": True})
    scheduler = object.__new__(Scheduler)
    scheduler.project_root = tmp_path
    scheduler.settings = settings
    job = Job(name="research", console_script="unused", module="unused", interval_min=60,
              market_hours_only=False)
    job.last_run_epoch = 100
    job.next_retry_epoch = 100000
    monkeypatch.setattr("bot.model_recovery.time.time", lambda: 5000)
    assert scheduler._job_eligible(job, now_tw(), once=False)
    assert job.next_retry_epoch == 0
    job.last_run_epoch = 5001
    job.next_retry_epoch = 8601
    assert not scheduler._job_eligible(job, now_tw(), once=False)
    client.health.assert_called_once()


def test_probe_failure_does_not_crash_or_retry_on_every_tick(tmp_path, monkeypatch):
    settings, path, client = blocked(tmp_path, monkeypatch, {})
    client.health.side_effect = TimeoutError("provider did not respond")
    assert refresh_blocked_model(tmp_path, settings, now=500) == 0
    assert refresh_blocked_model(tmp_path, settings, now=501) == 0
    assert read_json(path)["status"] == "blocked"
    client.health.assert_called_once()

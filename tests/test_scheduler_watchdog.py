from bot.scheduler_watchdog import decide


def test_missing_service_starts_and_budget_is_bounded():
    prior = {}
    for now in (1000, 1130, 1260):
        prior = decide({'alive': False}, prior, now)
        assert prior['action'] == 'start'
    prior = decide({'alive': False}, prior, 1390)
    assert prior['action'] == 'none' and prior['cooldown_until'] == 3190
    assert decide({'alive': False}, prior, 1800)['action'] == 'none'


def test_stale_process_needs_two_checks_and_valid_identity():
    observation = {'alive': True, 'created_epoch': 1, 'heartbeat_epoch': 10}
    first = decide(observation, {}, 1000)
    assert first['status'] == 'suspect'
    assert decide(observation, first, 1120)['action'] == 'restart'
    assert decide({**observation, 'identity_error': True}, first, 1120)['status'] == 'blocked'
    assert decide({**observation, 'ambiguous': True}, first, 1120)['action'] == 'none'


def test_startup_grace_and_active_jobs_prevent_stop():
    assert decide({'alive': True, 'created_epoch': 950}, {}, 1000)['status'] == 'starting'
    assert decide({'alive': False}, {'last_start_request': 950}, 1000)['status'] == 'starting'
    for field, expected in [('owned_job_active', 'waiting_for_job'), ('unconfirmed_job', 'blocked')]:
        assert decide({'alive': True, field: True}, {}, 1000)['status'] == expected


def test_responsive_model_with_quota_error_is_not_restarted():
    assert decide({'alive': True, 'responsive': True, 'quota_blocked': True}, {}, 1000)['status'] == 'healthy'


def test_fresh_scheduler_with_active_work_is_healthy_and_does_not_restart():
    observation = {'alive': True, 'created_epoch': 1, 'heartbeat_epoch': 990, 'owned_job_active': True}
    state = decide(observation, {}, 1000)
    assert state['status'] == 'healthy' and state['action'] == 'none'


def test_fresh_wrapper_heartbeat_cannot_hide_an_unresponsive_model():
    observation = {'alive': True, 'created_epoch': 1, 'heartbeat_epoch': 990, 'responsive': False}
    first = decide(observation, {}, 1000)
    assert first['status'] == 'suspect' and first['action'] == 'none'
    assert decide(observation, first, 1120)['action'] == 'restart'


def test_failed_account_probe_is_not_treated_as_responsive(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from bot import scheduler_watchdog as watchdog, codex_app_server

    monkeypatch.setattr(watchdog.time, 'time', lambda: 1000)
    monkeypatch.setattr(watchdog, 'driver', lambda *a: {
        'model': {'alive': True, 'created_epoch': 1, 'heartbeat_epoch': 990},
        'scheduler': {'alive': True, 'created_epoch': 1, 'heartbeat_epoch': 990},
    })
    monkeypatch.setattr(codex_app_server, 'CodexAppServerClient', lambda **kw: SimpleNamespace(
        health=lambda **kw: {'reachable': True, 'error': 'account RPC timed out'}))
    state = watchdog.run(tmp_path, observe_only=True)
    assert state['services']['model']['status'] == 'suspect'
    assert state['services']['model']['action'] == 'none'
    assert state['services']['scheduler']['status'] == 'healthy'


def test_file_lock_and_atomic_state(tmp_path):
    from bot.runtime_state import FileLock, atomic_json, read_json
    first, second = FileLock(tmp_path / 'lock'), FileLock(tmp_path / 'lock')
    assert first.acquire() and not second.acquire()
    first.release()
    assert second.acquire()
    second.release()
    atomic_json(tmp_path / 'state.json', {'done': True})
    assert read_json(tmp_path / 'state.json') == {'done': True}
    assert not list(tmp_path.glob('*.tmp'))

from types import SimpleNamespace

from bot.model_availability import ModelAvailability
from bot.runtime_state import FileLock, atomic_json, read_json


def client():
    calls = []
    return SimpleNamespace(url='ws://127.0.0.1:4500', model='gpt-6-sol', effort='max', timeout=300,
        health=lambda **kw: {'available': True, 'authenticated': True},
        generate=lambda text: (calls.append(text) or 'report', {}), calls=calls)


def test_quota_stops_subsequent_calls_across_instances(tmp_path, monkeypatch):
    c = client()
    c.generate = lambda text: (c.calls.append(text) or '', {'error': 'usage limit'})
    c.health = lambda **kw: {'available': True, 'next_retry_epoch': 5000}
    monkeypatch.setattr('bot.model_availability.time.time', lambda: 1000)
    assert ModelAvailability(tmp_path, c).generate('first')[1]['error_kind'] == 'quota'
    assert ModelAvailability(tmp_path, c).generate('second')[1]['deferred']
    assert c.calls == ['first']
    assert read_json(tmp_path / 'data/llm_availability.json')['next_retry_at'] == 5000


def test_probe_failure_does_not_generate_and_recovery_is_serial(tmp_path, monkeypatch):
    c = client()
    c.health = lambda **kw: {'available': False, 'quota_blocked': True, 'next_retry_epoch': 5000}
    monkeypatch.setattr('bot.model_availability.time.time', lambda: 1000)
    assert ModelAvailability(tmp_path, c).generate('blocked')[1]['deferred']
    assert not c.calls
    monkeypatch.setattr('bot.model_availability.time.time', lambda: 5001)
    c.health = lambda **kw: {'available': True}
    gate = ModelAvailability(tmp_path, c)
    with FileLock(tmp_path / 'data/.llm-call.lock'):
        assert gate.generate('busy')[1]['error_kind'] == 'busy'
    assert gate.generate('recovered')[0] == 'report'
    assert c.calls == ['recovered']
    assert read_json(gate.path)['status'] == 'available'


def test_empty_response_blocks_and_old_probe_lease_survives_restart(tmp_path, monkeypatch):
    c = client()
    monkeypatch.setattr('bot.model_availability.time.time', lambda: 1000)
    gate = ModelAvailability(tmp_path, c)
    atomic_json(gate.path, {'configuration': gate.key, 'status': 'probing', 'lease_until': 1200})
    assert gate.generate('unfinished')[1]['deferred']
    monkeypatch.setattr('bot.model_availability.time.time', lambda: 1201)
    c.generate = lambda text: ('', {})
    assert 'no usable' in gate.generate('empty')[1]['error']
    assert read_json(gate.path)['status'] == 'blocked'

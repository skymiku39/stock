import json

import pytest

from bot import codex_app_server as server


@pytest.mark.parametrize('status', ['completed', 'interrupted', 'failed', None])
def test_only_completed_turns_can_return_successful_model_output(monkeypatch, status):
    replies = iter([
        {'id': 1, 'result': {}},
        {'id': 2, 'result': {'thread': {'id': 'fixture'}}},
        {'id': 3, 'result': {'turn': {'id': 'turn-fixture'}}},
        {'method': 'item/agentMessage/delta', 'params': {'delta': 'partial or final output'}},
        {'method': 'turn/completed', 'params': {'turn': {'status': status}}},
    ])
    class Socket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def send(self, message):
            pass
        def recv(self, **kwargs):
            return json.dumps(next(replies))
    monkeypatch.setattr(server, 'connect', lambda *a, **kw: Socket())
    text, info = server.CodexAppServerClient(url='ws://127.0.0.1:4500').generate('fixture')
    if status == 'completed':
        assert text == 'partial or final output' and not info.get('error')
    else:
        assert not text and info.get('error')


def _socket(monkeypatch, events):
    sent = []
    replies = iter([
        {'id': 1, 'result': {}},
        {'id': 2, 'result': {'thread': {'id': 'fixture'}}},
        {'id': 3, 'result': {'turn': {'id': 'turn-fixture'}}},
        *events,
    ])

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def send(self, message):
            sent.append(json.loads(message))

        def recv(self, **kwargs):
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            return json.dumps(value)

    monkeypatch.setattr(server, 'connect', lambda *a, **kw: Socket())
    return sent


def test_final_item_overrides_commentary_and_streamed_fragments(monkeypatch):
    _socket(monkeypatch, [
        {'method': 'item/agentMessage/delta', 'params': {'delta': 'working...'}},
        {'method': 'item/completed', 'params': {'item': {
            'type': 'agentMessage', 'phase': 'commentary', 'text': 'working...'}}},
        {'method': 'item/agentMessage/delta', 'params': {'delta': '{"ok":true}'}},
        {'method': 'item/completed', 'params': {'item': {
            'type': 'agentMessage', 'phase': 'final_answer', 'text': '{"ok":true}'}}},
        {'method': 'turn/completed', 'params': {'turn': {'status': 'completed', 'items': []}}},
    ])
    text, info = server.CodexAppServerClient(url='ws://127.0.0.1:4500').generate('fixture')
    assert json.loads(text) == {'ok': True}
    assert not info.get('error')


def test_completed_turn_without_final_answer_is_rejected(monkeypatch):
    _socket(monkeypatch, [
        {'method': 'item/agentMessage/delta', 'params': {'delta': 'working...'}},
        {'method': 'turn/completed', 'params': {'turn': {'status': 'completed', 'items': [
            {'type': 'agentMessage', 'phase': 'commentary', 'text': 'working...'}]}}},
    ])
    text, info = server.CodexAppServerClient(url='ws://127.0.0.1:4500').generate('fixture')
    assert not text and 'without a final answer' in info['error']


@pytest.mark.parametrize('interrupt_error', [False, True])
def test_timeout_interrupts_only_the_owned_turn(monkeypatch, interrupt_error):
    sent = _socket(monkeypatch, [
        TimeoutError('fixture timeout'),
        {'id': 4, 'error': {'message': 'fixture cancellation failed'}} if interrupt_error
        else {'id': 4, 'result': {}},
    ])
    text, info = server.CodexAppServerClient(url='ws://127.0.0.1:4500').generate('fixture')
    interrupt = next(m for m in sent if m.get('method') == 'turn/interrupt')
    assert interrupt['params'] == {'threadId': 'fixture', 'turnId': 'turn-fixture'}
    assert not text and 'timed out' in info['error']
    assert info['interrupt_acknowledged'] is not interrupt_error


def test_unrelated_turn_events_cannot_supply_the_answer(monkeypatch):
    _socket(monkeypatch, [
        {'method': 'turn/completed', 'params': {'threadId': 'other', 'turn': {
            'status': 'completed', 'items': [{'type': 'agentMessage', 'text': 'wrong'}]}}},
        {'method': 'turn/completed', 'params': {'turn': {'id': 'other',
            'status': 'completed', 'items': [{'type': 'agentMessage', 'text': 'wrong'}]}}},
        {'method': 'turn/completed', 'params': {'turn': {'id': 'turn-fixture',
            'status': 'completed', 'items': [{'type': 'agentMessage', 'text': 'accepted'}]}}},
    ])
    text, info = server.CodexAppServerClient(url='ws://127.0.0.1:4500').generate('fixture')
    assert text == 'accepted' and not info.get('error')


def test_configured_deadline_reaches_the_model_client(tmp_path):
    from bot.config import Settings
    from bot.llm_analyzer import GeminiClient

    settings = Settings(_env_file=None, llm_provider='codex_app_server',
                        codex_app_server_timeout_seconds=123)
    client = GeminiClient(settings=settings, root=tmp_path)
    assert client._client.timeout == 123
    with pytest.raises(ValueError):
        Settings(_env_file=None, codex_app_server_timeout_seconds=0)


@pytest.mark.parametrize('account', [{'account': {'type': 'chatgpt'}},
                                     {'account': None, 'requiresOpenaiAuth': True}])
def test_liveness_probe_does_not_start_model_work(monkeypatch, account):
    sent = []
    replies = iter([{'id': 1, 'result': {}}, {'id': 2, 'result': account}])

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def send(self, message):
            sent.append(json.loads(message))

        def recv(self, **kwargs):
            return json.dumps(next(replies))

    monkeypatch.setattr(server, 'connect', lambda *a, **kw: Socket())
    result = server.CodexAppServerClient(url='ws://127.0.0.1:4500').health(full=False)
    assert result['reachable'] and 'error' not in result
    assert result['authenticated'] is bool(account.get('account'))
    assert [m['method'] for m in sent] == ['initialize', 'initialized', 'account/read']

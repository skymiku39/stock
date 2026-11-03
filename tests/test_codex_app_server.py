import json

import pytest

from bot import codex_app_server as server


@pytest.mark.parametrize('status', ['completed', 'interrupted', 'failed', None])
def test_only_completed_turns_can_return_successful_model_output(monkeypatch, status):
    replies = iter([
        {'id': 1, 'result': {}},
        {'id': 2, 'result': {'thread': {'id': 'fixture'}}},
        {'id': 3, 'result': {}},
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

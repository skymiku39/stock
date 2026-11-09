"""Cross-process model circuit with a single real-call recovery trial."""
from __future__ import annotations

import os
from pathlib import Path
import time

from bot.runtime_state import FileLock, atomic_json, read_json


def error_kind(error):
    value = str(error).lower()
    if any(s in value for s in ('usage limit', 'quota', 'rate limit', 'rate_limit', 'credits depleted')):
        return 'quota'
    if any(s in value for s in ('unauthorized', 'authentication', 'not logged', 'token expired', '401')):
        return 'authentication'
    return 'provider'


class ModelAvailability:
    def __init__(self, root, client):
        self.root, self.client = Path(root), client
        self.path = self.root / 'data/llm_availability.json'
        self.lock = FileLock(self.root / 'data/.llm-call.lock')
        self.key = f'{client.url}|{client.model}|{client.effort}'

    def deferred(self, reason, retry_at, kind='unavailable'):
        return '', {'error': reason, 'error_kind': kind, 'deferred': True, 'next_retry_at': retry_at}

    def generate(self, prompt):
        if not self.lock.acquire():
            return self.deferred('another model call is active', time.time() + 60, 'busy')
        try:
            now = time.time()
            state = read_json(self.path)
            if state.get('configuration') != self.key:
                state = {}
            if state.get('status') == 'probing' and state.get('lease_until', 0) > now:
                return self.deferred('previous model call recovery lease has not expired', state['lease_until'], 'busy')
            if state.get('status') == 'blocked' and state.get('next_retry_at', 0) > now:
                return self.deferred(state.get('reason', 'model paused'), state['next_retry_at'], state.get('error_kind', 'unavailable'))
            if state.get('status') != 'available' or now - state.get('checked_at', 0) >= 1800:
                health = self.client.health(timeout=10)
                state.update(configuration=self.key, checked_at=now, health=health)
                if not health.get('available'):
                    reason = ('model quota unavailable' if health.get('quota_blocked') else
                              'model authentication unavailable' if not health.get('authenticated') else
                              'model service/configuration/limits not verified')
                    retry = health.get('next_retry_epoch', now + 1800)
                    state.update(status='blocked', reason=reason, error_kind='quota' if health.get('quota_blocked') else 'unavailable', next_retry_at=retry)
                    atomic_json(self.path, state)
                    return self.deferred(reason, retry, state['error_kind'])
            state.update(configuration=self.key, status='probing', lease_until=now + self.client.timeout + 60, owner_pid=os.getpid())
            atomic_json(self.path, state)
            try:
                text, info = self.client.generate(prompt)
            except Exception as exc:
                text, info = '', {'error': str(exc)}
            info = dict(info)
            if not isinstance(text, str) or not text.strip() or info.get('error'):
                error = str(info.get('error') or 'model returned no usable output')
                kind = error_kind(error)
                retry = time.time() + 1800
                if kind == 'quota':
                    health = self.client.health(timeout=10)
                    retry = health.get('next_retry_epoch', retry)
                    state['health'] = health
                state.update(status='blocked', error_kind=kind, reason=error, next_retry_at=retry)
                info.update(error=error, error_kind=kind, next_retry_at=retry)
            else:
                state.update(status='available', reason='', error_kind='', next_retry_at=0, last_success_at=time.time())
            state.pop('lease_until', None)
            atomic_json(self.path, state)
            return text, info
        finally:
            self.lock.release()

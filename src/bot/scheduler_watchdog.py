"""Independent, bounded Windows service recovery; no model generation."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time

from bot.runtime_state import FileLock, StateBusy, atomic_json, read_json
from bot.utils import now_tw

SERVICES = ('model', 'scheduler')


def epoch(value):
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return 0.0


def decide(observation, prior, now):
    """Pure policy. Identity is verified again by the Windows driver on mutation."""
    state = dict(prior)
    state['checked_at_epoch'] = now
    state['action'] = 'none'
    if observation.get('ambiguous') or observation.get('identity_error'):
        state.update(status='blocked', reason='process identity could not be established')
        return state
    if now < float(state.get('cooldown_until', 0)):
        state.update(status='cooldown', reason='recovery budget exhausted')
        return state
    history = [t for t in state.get('restart_history', []) if now - t < 600]
    state['restart_history'] = history
    alive = observation.get('alive', False)
    started = float(observation.get('created_epoch') or 0)
    last_start = max(started, float(state.get('last_start_request', 0)))
    if last_start and now - last_start < 120:
        state.update(status='starting', stale_checks=0, reason='startup grace period')
        return state
    if observation.get('unconfirmed_job'):
        state.update(status='blocked', reason='cannot prove child process identity or deadline')
        return state
    fresh = now - float(observation.get('heartbeat_epoch') or 0) <= 180
    responsive = observation.get('responsive', fresh)
    # A protocol probe, when present, is authoritative. The wrapper can keep
    # writing heartbeats even while the model's RPC handler is unresponsive.
    if alive and responsive:
        state.update(status='healthy', stale_checks=0, reason='process and liveness check passed')
        return state
    if observation.get('owned_job_active'):
        state.update(status='waiting_for_job', reason='owned research process still within deadline', stale_checks=0)
        return state
    stale = int(state.get('stale_checks', 0)) + 1 if alive else 0
    state['stale_checks'] = stale
    if alive and stale < 2:
        state.update(status='suspect', reason='waiting for second stale observation')
        return state
    if len(history) >= 3:
        state.update(status='cooldown', cooldown_until=now + 1800, reason='three recovery requests in ten minutes')
        return state
    state.update(action='restart' if alive else 'start', status='recovering',
                 reason='stale liveness' if alive else 'process absent',
                 restart_history=history + [now], last_start_request=now, stale_checks=0)
    return state


def driver(root, action, service=None, observed=None):
    command = ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
               '-File', str(root / 'scripts/stock_service_control.ps1'), '-Root', str(root), '-Action', action]
    if service:
        command += ['-Service', service]
    if observed:
        command += ['-ExpectedPid', str(observed['pid']), '-ExpectedCreated', observed['created_at']]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8-sig',
                            errors='replace', timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-1500:])
    return json.loads(result.stdout)


def run(root, *, observe_only=False):
    root = Path(root).resolve()
    path = root / 'data/watchdog_status.json'
    with FileLock(root / 'data/.watchdog.lock'):
        previous = read_json(path)
        maintenance = read_json(root / 'data/watchdog_maintenance.json')
        stamp = now_tw().isoformat()
        if maintenance.get('paused'):
            result = {'checked_at': stamp, 'status': 'maintenance', 'reason': maintenance.get('reason', '')}
            if not observe_only:
                atomic_json(path, result)
            return result
        observations = driver(root, 'inspect')
        model = observations.get('model', {})
        if model.get('alive') and not model.get('identity_error'):
            from bot.codex_app_server import CodexAppServerClient
            try:
                health = CodexAppServerClient(url='ws://127.0.0.1:4500').health(timeout=8, full=False)
                model['responsive'] = bool(health.get('reachable') and not health.get('error'))
            except Exception:
                model['responsive'] = False
        services = dict(previous.get('services', {}))
        events = []
        for name in SERVICES:
            observation = observations.get(name, {})
            state = decide(observation, previous.get('services', {}).get(name, {}), time.time())
            state['observation'] = observation
            services[name] = state
            # Persist budget BEFORE changing an external process; interrupted ticks cannot retry without accounting.
            if not observe_only:
                atomic_json(path, {'checked_at': stamp, 'services': services})
            if state['action'] != 'none' and not observe_only:
                try:
                    if state['action'] == 'restart':
                        driver(root, 'stop', name, observation)
                    driver(root, 'start', name)
                    events.append({'service': name, 'at': stamp, 'action': state['action'], 'reason': state['reason']})
                except Exception as exc:
                    state.update(status='blocked', error=str(exc), reason='recovery could not be confirmed')
                    events.append({'service': name, 'at': stamp, 'error': str(exc)})
        result = {'checked_at': stamp, 'status': 'ok' if all(s['status'] == 'healthy' for s in services.values()) else 'degraded',
                  'services': services, 'observe_only': observe_only}
        if not observe_only:
            atomic_json(path, result)
            if events:
                log = root / 'log/watchdog-events.jsonl'
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open('a', encoding='utf-8') as handle:
                    for event in events:
                        handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--observe-only', action='store_true')
    parser.add_argument('--maintenance', choices=('on', 'off'))
    parser.add_argument('--reason', default='operator maintenance')
    args = parser.parse_args(argv)
    if args.maintenance:
        atomic_json(args.root / 'data/watchdog_maintenance.json',
                    {'paused': args.maintenance == 'on', 'reason': args.reason, 'updated_at': now_tw().isoformat()})
        return 0
    try:
        result = run(args.root, observe_only=args.observe_only)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] in {'ok', 'maintenance'} else 3
    except StateBusy:
        return 4
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    sys.exit(main())

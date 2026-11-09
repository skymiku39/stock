"""Reuse only successful, exact-input briefs with their original provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bot.daily_collection import collection_period
from bot.runtime_state import atomic_json, read_json
from bot.utils import now_tw


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def key_for(root, prompt, variables, metadata, client):
    from bot.source_registry import SourceRegistry, resolve_source_document_path
    with SourceRegistry(root) as registry:
        source = registry.db.get_source_document(metadata['input_source_document_id'])
        receipt = json.loads(resolve_source_document_path(Path(root), source).read_bytes())
    period = metadata.get('collection_period') or collection_period(now_tw())
    date = str(variables.get('date') or variables.get('asof_date') or now_tw().date().isoformat())
    settings = getattr(client, 'settings', None)
    payload = {'date': date, 'period': period, 'task': prompt.id, 'template': prompt.template,
               'version': prompt.version, 'max_output_tokens': prompt.max_output_tokens,
               'temperature': prompt.temperature, 'model': client.model,
               'provider': getattr(settings, 'llm_provider', ''),
               'effort': getattr(settings, 'codex_app_server_effort', ''),
               'variables': variables, 'source_hashes': receipt['source_hashes']}
    return date, digest(payload)


def prepare(root, prompt, variables, metadata, client):
    date, key = key_for(root, prompt, variables, metadata, client)
    atomic_json(Path(root) / 'data/brief_pending' / date / (prompt.id + '.json'),
                {'task': prompt.id, 'variables': variables, 'metadata': metadata,
                 'key': key, 'prepared_at': now_tw().isoformat()})
    return Path(root) / 'data/brief_cache' / date / (prompt.id + '-' + key + '.json')


def load(root, path):
    from bot.pipeline_brief import validate_saved_brief
    from bot.source_registry import SourceRegistry
    saved = read_json(path)
    text = saved.get('text')
    info = saved.get('info') or {}
    metadata = info.get('input_provenance') or {}
    if not isinstance(text, str) or not text.strip() or digest(text) != saved.get('output_sha256'):
        return None
    if not metadata.get('analysis_id') or not validate_saved_brief(root, info.get('prompt_id'), metadata):
        return None
    with SourceRegistry(root) as registry:
        row = registry.db.conn.execute('SELECT raw_output,success FROM llm_analysis_history WHERE id=?',
                                       (metadata['analysis_id'],)).fetchone()
        # The DB mirror caps raw_output at 8000 chars. Long outputs are not reusable without an exact stored execution.
        if row is None or not row['success'] or row['raw_output'] != text:
            return None
    reused = {**info, 'reused': True, 'reused_at': now_tw().isoformat()}
    reused['input_provenance'] = {**metadata, 'reused': True,
                                 'original_generated_at': saved['generated_at']}
    return text, reused


def save(path, text, info):
    if isinstance(text, str) and text.strip() and not info.get('error') and not info.get('deferred'):
        atomic_json(path, {'text': text, 'output_sha256': digest(text), 'info': info,
                           'generated_at': now_tw().isoformat()})

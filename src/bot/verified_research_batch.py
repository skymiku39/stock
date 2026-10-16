"""Resumable, serial research of an exact backlog using replayed official rows."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from bot.evidence import make_evidence, persist_evidence_items
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.utils import now_tw


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def backlog(run):
    selected = set(run['auto_research_batch']['selected_tickers'])
    return list(dict.fromkeys(t for t in run['focus_tickers'] if t not in selected))


def matching_rows(raw, ticker):
    if not isinstance(raw, list):
        return []
    keys = ('公司代號', '證券代號', 'Code')
    return [r for r in raw if isinstance(r, dict) and
            any(str(r.get(k, '')).strip() == ticker for k in keys)]


def collect(root, tickers):
    result = {t: [] for t in tickers}
    excluded = []
    with SourceRegistry(root) as registry:
        rows = registry.db.conn.execute(
            "SELECT * FROM source_documents WHERE source LIKE 'monthly_revenue_%' "
            "OR source LIKE 'valuation_%' OR source LIKE 'quarterly_financials_%' "
            "OR source LIKE 'dividend_%' ORDER BY retrieved_at DESC, document_id"
        ).fetchall()
        seen = set()
        for row in rows:
            if row['source'] in seen:
                continue
            seen.add(row['source'])
            source = registry.db.get_source_document(row['document_id'])
            try:
                if urlparse(source.url).hostname not in {'openapi.twse.com.tw', 'www.tpex.org.tw', 'mopsfin.twse.com.tw'}:
                    raise ValueError('unsupported official source host')
                if source.quality_status not in {'ok', 'partial'}:
                    raise ValueError('unusable source quality')
                content = resolve_source_document_path(root, source).read_bytes()
                if hashlib.sha256(content).hexdigest() != source.content_hash:
                    raise ValueError('raw hash mismatch')
                raw = json.loads(content)
                for ticker in tickers:
                    for record in matching_rows(raw, ticker):
                        # Keep the exact original values and their original period labels.
                        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
                        packet = make_evidence('official_company_row', source=source.source,
                            source_ref=source.document_id + ':' + ticker + ':' + hashlib.sha256(encoded.encode()).hexdigest(),
                            title=ticker + ' official source row', url=source.url,
                            excerpt=encoded, as_of=source.as_of, retrieved_at=source.retrieved_at,
                            source_document_id=source.document_id, quality_status=source.quality_status,
                            provenance_status='exact')
                        persist_evidence_items(registry.db, [packet])
                        result[ticker].append({'evidence_id': packet['id'], 'source_document_id': source.document_id,
                            'sha256': source.content_hash, 'url': source.url, 'retrieved_at': source.retrieved_at,
                            'row': record})
            except (ValueError, OSError, TypeError) as exc:
                excluded.append({'source_document_id': source.document_id, 'reason': str(exc)})
    return result, excluded


def validate_report(report, item):
    if not isinstance(report, dict) or report.get('ticker') != item['ticker']:
        raise ValueError('ticker mismatch')
    if not isinstance(report.get('summary'), str) or not report['summary'].strip():
        raise ValueError('missing summary')
    if not isinstance(report.get('limitations'), list) or not report['limitations']:
        raise ValueError('missing limitations')
    observations = report.get('observations')
    if not isinstance(observations, list) or not observations:
        raise ValueError('missing observations')
    sources = {s['evidence_id']: s['row'] for s in item['sources']}
    for observation in observations:
        row = sources.get(observation.get('evidence_id'), {})
        field = observation.get('field')
        if field not in row or json.dumps(row[field], ensure_ascii=False) != json.dumps(observation.get('value'), ensure_ascii=False):
            raise ValueError('observation is not an exact source field')


def revalidate(root, items):
    with SourceRegistry(root) as registry:
        for item in items:
            for entry in item['sources']:
                source = registry.db.get_source_document(entry['source_document_id'])
                if source is None or source.quality_status not in {'ok', 'partial'}:
                    raise ValueError('source became unavailable')
                raw = resolve_source_document_path(root, source).read_bytes()
                if hashlib.sha256(raw).hexdigest() != entry['sha256'] or source.content_hash != entry['sha256']:
                    raise ValueError('source changed before model call')
                if entry['row'] not in matching_rows(json.loads(raw), item['ticker']):
                    raise ValueError('company row changed before model call')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--source-run', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--batch-size', type=int, default=8)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not 1 <= args.batch_size <= 12:
        parser.error('batch size must be between 1 and 12')
    original = json.loads((root / 'data/pipeline_runs' / args.source_run / 'run.json').read_text(encoding='utf-8'))
    tickers = backlog(original)
    out = root / 'data/verified_research_batches' / args.source_run
    attempt_id = now_tw().strftime('%Y%m%d_%H%M%S_%f')
    if (out / 'status.json').exists():
        previous = json.loads((out / 'status.json').read_text(encoding='utf-8'))
        write_json(out / 'attempts' / (attempt_id + '-previous-status.json'), previous)
    inputs, exclusions = collect(root, tickers)
    state = {'source_run': args.source_run, 'requested': len(tickers), 'started_at': now_tw().isoformat(),
             'scope': 'official financial and valuation rows only; model conclusions unverified',
             'source_exclusions': exclusions, 'results': {}}
    pending = []
    for ticker in tickers:
        item = {'ticker': ticker, 'sources': inputs[ticker]}
        if not item['sources']:
            state['results'][ticker] = {'status': 'excluded', 'reason': 'no verifiable official company row; no model call'}
        else:
            saved = out / 'reports' / (ticker + '.json')
            try:
                report = json.loads(saved.read_text(encoding='utf-8'))
                if report.get('input') != item:
                    raise ValueError('inputs changed')
                validate_report(report, item)
                revalidate(root, [item])
                state['results'][ticker] = {'status': 'completed', 'reused': True,
                                            'evidence_count': len(item['sources'])}
            except (OSError, ValueError, TypeError, AttributeError):
                pending.append(item)
    write_json(out / 'inputs.json', pending)
    write_json(out / 'status.json', state)
    print(f'Requested {len(tickers)}; eligible {len(pending)}; excluded {len(tickers)-len(pending)}', flush=True)
    if not args.execute:
        return 0
    from bot.config import Settings
    from bot.llm_analyzer import GeminiClient, gemini_call, extract_json
    settings = Settings(_env_file=str(root / '.env'))
    client = GeminiClient(api_key=settings.gemini_api_key, model=settings.gemini_model, settings=settings, root=root)
    if not client.enabled:
        raise RuntimeError('model unavailable')
    for offset in range(0, len(pending), args.batch_size):
        batch = pending[offset:offset + args.batch_size]
        batch_id = str(offset // args.batch_size + 1).zfill(3)
        try:
            revalidate(root, batch)
            variables = {'inputs_json': json.dumps(batch, ensure_ascii=False, allow_nan=False)}
            with SourceRegistry(root) as registry:
                receipt = registry.capture_document('verified_research_input', json.dumps(variables, ensure_ascii=False),
                    source_ref=args.source_run + ':' + attempt_id + ':' + batch_id, suffix='json', kind='llm_input_snapshot')
            evidence_ids = [s['evidence_id'] for item in batch for s in item['sources']]
            raw, info = gemini_call('verified_research_batch', client=client, root=root,
                metadata={'task': 'verified_research_batch', 'evidence_ids': evidence_ids,
                    'input_source_document_id': receipt.document_id}, **variables)
            write_json(out / ('response-' + attempt_id + '-' + batch_id + '.json'), {'raw': raw, 'info': info})
            parsed = extract_json(raw or '')
            reports = parsed.get('reports', []) if isinstance(parsed, dict) else []
            revalidate(root, batch)
            for item in batch:
                matches = [r for r in reports if isinstance(r, dict) and r.get('ticker') == item['ticker']]
                try:
                    if len(matches) != 1:
                        raise ValueError('missing or duplicate ticker output')
                    validate_report(matches[0], item)
                    report = {**matches[0], 'input': item, 'input_receipt': receipt.document_id,
                              'conclusions_verified': False, 'scope': state['scope'], 'generated_at': now_tw().isoformat()}
                    write_json(out / 'reports' / (item['ticker'] + '.json'), report)
                    state['results'][item['ticker']] = {'status': 'completed', 'evidence_count': len(item['sources'])}
                except (ValueError, TypeError, AttributeError) as exc:
                    state['results'][item['ticker']] = {'status': 'rejected_output', 'reason': str(exc)}
        except Exception as exc:
            for item in batch:
                state['results'][item['ticker']] = {'status': 'failed', 'reason': str(exc)}
        state['updated_at'] = now_tw().isoformat()
        write_json(out / 'status.json', state)
        print(f'Batch {batch_id}: ' + str({i['ticker']: state['results'][i['ticker']]['status'] for i in batch}), flush=True)
    state['ended_at'] = now_tw().isoformat()
    write_json(out / 'status.json', state)
    return 2 if any(r['status'] in {'failed', 'rejected_output'} for r in state['results'].values()) else 0


if __name__ == '__main__':
    raise SystemExit(main())

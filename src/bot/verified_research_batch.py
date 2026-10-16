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
    batch = run.get('auto_research_batch', {})
    selected = set(batch.get('selected_tickers', [])) - set(batch.get('no_result_tickers', []))
    selected = {t for t in selected if not any(str(error).startswith(f'auto_research_{t}:') for error in run.get('errors', []))}
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
                if entry.get('format'):
                    from bot.foreign_research_sources import validate_entry
                    validate_entry(root, registry.db, item['ticker'], entry)
                    continue
                source = registry.db.get_source_document(entry['source_document_id'])
                if source is None or source.quality_status not in {'ok', 'partial'}:
                    raise ValueError('source became unavailable')
                raw = resolve_source_document_path(root, source).read_bytes()
                if hashlib.sha256(raw).hexdigest() != entry['sha256'] or source.content_hash != entry['sha256']:
                    raise ValueError('source changed before model call')
                if entry['row'] not in matching_rows(json.loads(raw), item['ticker']):
                    raise ValueError('company row changed before model call')


def _main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--source-run')
    parser.add_argument('--tickers', nargs='+', help='Explicit ticker list; quote foreign symbols with market suffix')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--refresh-sources', action='store_true')
    parser.add_argument('--selected-only', action='store_true', help='Respect the daily scheduler ticker budget')
    parser.add_argument('--max-attempts', type=int, default=2)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--foreign', action='store_true', help='Research the exact previously excluded foreign backlog')
    parser.add_argument('--batch-size', type=int, default=8)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not 1 <= args.batch_size <= 12:
        parser.error('batch size must be between 1 and 12')
    if args.tickers:
        tickers = list(dict.fromkeys(args.tickers))
    elif args.source_run:
        if Path(args.source_run).name != args.source_run:
            parser.error('source run must be a directory name')
        original = json.loads((root / 'data/pipeline_runs' / args.source_run / 'run.json').read_text(encoding='utf-8'))
        tickers = backlog(original)
        # A selected foreign ticker may have failed the domestic adapter.
        tickers = list(dict.fromkeys(tickers + [t for t in original['focus_tickers'] if ' ' in t]))
        if args.selected_only:
            batch = original.get('auto_research_batch', {})
            missing = set(backlog(original))
            tickers = [t for t in batch.get('selected_tickers', []) if ' ' in t or t in missing]
    else:
        parser.error('provide --source-run or --tickers')
    import re
    if any(not re.fullmatch(r'[A-Za-z0-9.^-]+(?: (?:US|JP|KS|GY))?', t) for t in tickers):
        parser.error('invalid ticker or market suffix')
    if args.foreign:
        tickers = [t for t in tickers if ' ' in t]
    out = args.output_dir or root / 'data/verified_research_batches' / (args.source_run or 'explicit') / ('automated-foreign' if args.foreign else 'automated') / now_tw().date().isoformat()
    if not out.is_absolute():
        out = root / out
    attempt_id = now_tw().strftime('%Y%m%d_%H%M%S_%f')
    if (out / 'status.json').exists():
        previous = json.loads((out / 'status.json').read_text(encoding='utf-8'))
        write_json(out / 'attempts' / (attempt_id + '-previous-status.json'), previous)
    foreign = [t for t in tickers if ' ' in t]
    inputs, exclusions = collect(root, [t for t in tickers if t not in foreign])
    if foreign:
        from bot.foreign_research_sources import collect as collect_foreign
        foreign_inputs, foreign_exclusions = collect_foreign(root, foreign, refresh=args.refresh_sources)
        inputs.update(foreign_inputs)
        exclusions.extend(foreign_exclusions)
    state = {'source_run': args.source_run, 'requested': len(tickers), 'started_at': now_tw().isoformat(),
             'scope': 'official financial and valuation rows only; model conclusions unverified',
             'source_exclusions': exclusions, 'results': {}}
    from bot.verified_research_audit import verify_saved, summarize
    cache = root / 'data/verified_research_cache' / now_tw().date().isoformat()
    pending = []
    for ticker in tickers:
        item = {'ticker': ticker, 'sources': inputs[ticker]}
        saved = out / 'reports' / (ticker + '.json')
        if not item['sources']:
            state['results'][ticker] = {'status': 'excluded', 'reason': 'no verifiable official company row; no model call'}
        else:
            try:
                candidate = saved if saved.exists() else cache / (ticker + '.json')
                report = json.loads(candidate.read_text(encoding='utf-8'))
                verify_saved(root, report, item)
                if candidate != saved:
                    write_json(saved, report)
                write_json(cache / (ticker + '.json'), report)
                state['results'][ticker] = {'status': 'completed', 'reused': True,
                                            'evidence_count': len(item['sources'])}
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                pending.append(item)
                state['results'][ticker] = {'status': 'pending'}
        if saved.exists() and state['results'][ticker]['status'] != 'completed':
            archived = out / 'attempts' / attempt_id / 'reports' / saved.name
            archived.parent.mkdir(parents=True, exist_ok=True)
            saved.replace(archived)
    write_json(out / 'inputs.json', [{'ticker': t, 'sources': inputs[t]} for t in tickers if inputs[t]])
    write_json(out / 'status.json', state)
    summarize(root, out, state)
    reused = sum(r['status'] == 'completed' for r in state['results'].values())
    excluded_count = sum(r['status'] == 'excluded' for r in state['results'].values())
    print(f'Requested {len(tickers)}; pending {len(pending)}; reused {reused}; excluded {excluded_count}', flush=True)
    if not args.execute or not pending:
        summary = summarize(root, out, state)
        if any(r['status'] == 'verification_failed' for r in state['results'].values()):
            return 2
        return 3 if excluded_count else 0
    from bot.config import Settings
    from bot.llm_analyzer import GeminiClient, gemini_call, extract_json
    settings = Settings(_env_file=str(root / '.env'))
    client = GeminiClient(api_key=settings.gemini_api_key, model=settings.gemini_model, settings=settings, root=root)
    if not client.enabled:
        for item in pending:
            state['results'][item['ticker']] = {'status': 'failed', 'reason': 'model unavailable'}
        summarize(root, out, state)
        return 2
    for offset in range(0, len(pending), args.batch_size):
        batch = pending[offset:offset + args.batch_size]
        batch_id = str(offset // args.batch_size + 1).zfill(3)
        try:
            revalidate(root, batch)
            variables = {'inputs_json': json.dumps(batch, ensure_ascii=False, allow_nan=False)}
            with SourceRegistry(root) as registry:
                receipt = registry.capture_document('verified_research_input', json.dumps(variables, ensure_ascii=False),
                    source_ref=(args.source_run or 'explicit') + ':' + attempt_id + ':' + batch_id, suffix='json', kind='llm_input_snapshot')
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
                    verify_saved(root, report, item)
                    write_json(out / 'reports' / (item['ticker'] + '.json'), report)
                    write_json(cache / (item['ticker'] + '.json'), report)
                    state['results'][item['ticker']] = {'status': 'completed', 'evidence_count': len(item['sources'])}
                except (ValueError, TypeError, AttributeError, KeyError, OSError) as exc:
                    state['results'][item['ticker']] = {'status': 'rejected_output', 'reason': str(exc)}
        except Exception as exc:
            for item in batch:
                state['results'][item['ticker']] = {'status': 'failed', 'reason': str(exc)}
        state['updated_at'] = now_tw().isoformat()
        write_json(out / 'status.json', state)
        print(f'Batch {batch_id}: ' + str({i['ticker']: state['results'][i['ticker']]['status'] for i in batch}), flush=True)
    state['ended_at'] = now_tw().isoformat()
    write_json(out / 'status.json', state)
    summary = summarize(root, out, state)
    if any(r['status'] in {'failed', 'rejected_output', 'verification_failed'} for r in state['results'].values()):
        return 2
    return 0 if summary['complete'] else 3


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--help' in argv or '-h' in argv:
        return _main(argv)
    from bot.verified_research_audit import exclusive_run
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--max-attempts', type=int, default=2)
    args, _ = parser.parse_known_args(argv)
    if not 1 <= args.max_attempts <= 3:
        parser.error('max attempts must be between 1 and 3')
    try:
        with exclusive_run(args.root.resolve()):
            for attempt in range(args.max_attempts):
                result = _main(argv)
                if result != 2:
                    return result
            return result
    except RuntimeError as exc:
        if str(exc) != 'another verified research process is running':
            raise
        print(str(exc), file=sys.stderr)
        return 4


if __name__ == '__main__':
    raise SystemExit(main())

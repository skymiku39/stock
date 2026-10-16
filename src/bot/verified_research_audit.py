"""Machine-readable verification for unattended, resumable research."""
import json
import os
from collections import Counter
from contextlib import contextmanager

from bot.source_registry import SourceRegistry
from bot.utils import now_tw


@contextmanager
def exclusive_run(root):
    path = root / 'data/verified_research.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('another verified research process is running') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def verify_saved(root, report, item):
    from bot.foreign_research_sources import read_source
    from bot.verified_research_batch import revalidate, validate_report
    if report.get('input') != item or report.get('conclusions_verified') is not False:
        raise ValueError('saved report input or conclusion status mismatch')
    validate_report(report, item)
    revalidate(root, [item])
    with SourceRegistry(root) as registry:
        import hashlib
        for entry in item['sources']:
            evidence = registry.db.get_evidence(entry['evidence_id'])
            encoded = json.dumps(entry['row'], ensure_ascii=False, allow_nan=False,
                                 sort_keys=entry.get('format') != 'official_excerpt')
            if (evidence is None or evidence.source_document_id != entry['source_document_id']
                    or evidence.excerpt != encoded[:2000]):
                raise ValueError('stored evidence does not match the original model input')
            meta = json.loads(evidence.metadata_json)
            if meta.get('row_sha256') and meta['row_sha256'] != hashlib.sha256(encoded.encode('utf-8')).hexdigest():
                raise ValueError('full evidence row hash mismatch')
        source, raw = read_source(root, registry.db, report['input_receipt'])
        if source.source != 'verified_research_input' or item not in json.loads(json.loads(raw)['inputs_json']):
            raise ValueError('model input receipt mismatch')
        runs = registry.db.conn.execute(
            "SELECT run_id FROM llm_runs WHERE status='ok' AND json_extract(metadata_json,'$.input_source_document_id')=?",
            (source.document_id,)).fetchall()
        expected = {e['evidence_id'] for e in item['sources']}
        if not any(expected <= {e.evidence_id for e in registry.db.list_llm_run_evidence(run['run_id'])} for run in runs):
            raise ValueError('successful model evidence links missing')


def summarize(root, out, state):
    from bot.verified_research_batch import write_json
    inputs = {i['ticker']: i for i in json.loads((out / 'inputs.json').read_text(encoding='utf-8'))}
    documents, receipts, evidence = set(), set(), set()
    details = []
    for ticker, result in state['results'].items():
        if result['status'] != 'completed':
            continue
        try:
            report = json.loads((out / 'reports' / (ticker + '.json')).read_text(encoding='utf-8'))
            verify_saved(root, report, inputs[ticker])
            receipts.add(report['input_receipt'])
            periods = []
            for entry in inputs[ticker]['sources']:
                evidence.add(entry['evidence_id'])
                for key in ('source_document_id', 'directory_source_id', 'submissions_source_id', 'identity_source_id'):
                    if entry.get(key):
                        documents.add(entry[key])
                documents.update(entry.get('discovery_source_ids', []))
                periods.append(entry['row'].get('end', entry['row'].get('period_excerpt', 'see original row')))
            details.append({'ticker': ticker, 'periods': list(dict.fromkeys(periods))})
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
            state['results'][ticker] = {'status': 'verification_failed', 'reason': str(exc)}
            failed = out / 'reports' / (ticker + '.json')
            if failed.exists():
                destination = out / 'attempts' / ('verification-' + now_tw().strftime('%Y%m%d_%H%M%S_%f')) / failed.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                failed.replace(destination)
    counts = dict(Counter(r['status'] for r in state['results'].values()))
    summary = {'verified_at': now_tw().isoformat(), 'requested': state['requested'], 'counts': counts,
               'complete': counts.get('completed', 0) == state['requested'],
               'raw_source_documents': len(documents), 'model_input_receipts': len(receipts),
               'exact_evidence_entries': len(evidence), 'scope': state['scope'],
               'excluded_tickers': [t for t,r in state['results'].items() if r['status']=='excluded'],
               'results': state['results'], 'periods': details}
    write_json(out / 'verification-summary.json', summary)
    write_json(out / 'status.json', state)
    return summary

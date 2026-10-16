import json
from types import SimpleNamespace

import pytest

from bot.official_release_discovery import release_links, publication
from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.verified_research_audit import exclusive_run, verify_saved
from bot.verified_research_batch import main


def test_discovery_ranks_releases_and_rejects_other_hosts():
    html = '''<a href="/ir/items/tannshinn2025Q3.pdf">old</a>
    <a href="https://evil.example/tannshinn2099Q4.pdf">bad</a>
    <a href="/ir/items/tannshinn2026Q1.pdf">new</a>'''
    assert release_links(html, 'https://www.ibiden.com/ir/', '4062 JP') == [
        'https://www.ibiden.com/ir/items/tannshinn2026Q1.pdf',
        'https://www.ibiden.com/ir/items/tannshinn2025Q3.pdf']
    assert publication('July. 30. 2026 Q2 Results')[1].isoformat() == '2026-07-30'
    assert publication('Neubiberg, 5 August 2026')[1].isoformat() == '2026-08-05'
    with pytest.raises(ValueError):
        publication('unknown date')


def test_concurrent_runs_are_rejected_and_lock_released(tmp_path):
    with exclusive_run(tmp_path):
        with pytest.raises(RuntimeError):
            with exclusive_run(tmp_path):
                pass
    with exclusive_run(tmp_path):
        pass


def test_unattended_retries_resumes_audits_and_quarantines_invalid_inputs(tmp_path, monkeypatch):
    from bot import config, llm_analyzer
    with SourceRegistry(tmp_path) as registry:
        source = registry.capture_document('monthly_revenue_上市', json.dumps([
            {'公司代號': '2330', '營收': '100', '資料年月': '11509'}]),
            source_url='https://openapi.twse.com.tw/v1/opendata/t187ap05_L', suffix='json')
        raw_path = resolve_source_document_path(tmp_path, source)
    monkeypatch.setattr(config, 'Settings', lambda **kw: SimpleNamespace(gemini_api_key='', gemini_model='test'))
    monkeypatch.setattr(llm_analyzer, 'GeminiClient', lambda **kw: SimpleNamespace(enabled=True))
    calls = []
    def generate(prompt, *, metadata, inputs_json, **kwargs):
        batch = json.loads(inputs_json); calls.append(batch)
        with SourceRegistry(tmp_path) as registry:
            run_id = 'test-' + str(len(calls))
            registry.db.conn.execute("INSERT INTO llm_runs(run_id,status,metadata_json) VALUES (?,'ok',?)",
                                     (run_id, json.dumps(metadata)))
            for eid in metadata['evidence_ids']:
                registry.db.conn.execute('INSERT INTO llm_run_evidence(run_id,evidence_id) VALUES (?,?)', (run_id,eid))
            registry.db.conn.commit()
        reports = [{'ticker': i['ticker'], 'summary': 'limited', 'limitations': ['historical'],
                    'observations': [{'evidence_id': i['sources'][0]['evidence_id'], 'field': '營收',
                                      'value': 'wrong' if len(calls)==1 else '100'}]} for i in batch]
        return json.dumps({'reports':reports}), {'test':True}
    monkeypatch.setattr(llm_analyzer, 'gemini_call', generate)
    out = tmp_path / 'output'
    args = ['--root',str(tmp_path),'--tickers','2330','--output-dir',str(out),'--execute']
    assert main(args) == 0
    assert len(calls)==2
    summary=json.loads((out/'verification-summary.json').read_text(encoding='utf-8'))
    assert summary['complete'] and summary['counts']=={'completed':1}
    assert main(args)==0 and len(calls)==2
    next_run = list(args)
    next_run[next_run.index('--output-dir') + 1] = str(tmp_path / 'another-run')
    assert main(next_run)==0 and len(calls)==2
    report=json.loads((out/'reports/2330.json').read_text(encoding='utf-8'))
    report['input_receipt']='missing'
    with pytest.raises(ValueError): verify_saved(tmp_path,report,report['input'])
    raw_path.write_text('[]',encoding='utf-8')
    assert main(args)==3 and len(calls)==2
    assert not (out/'reports/2330.json').exists()
    assert list((out/'attempts').glob('*/reports/2330.json'))
    assert not json.loads((out/'verification-summary.json').read_text(encoding='utf-8'))['complete']


def test_sec_failure_does_not_block_non_us_collection(tmp_path, monkeypatch):
    from bot import foreign_research_sources as foreign, official_release_discovery as discovery
    monkeypatch.delenv('SEC_USER_AGENT', raising=False)
    def missing(ticker, fetch, cutoff):
        raise ValueError('official index changed')
    monkeypatch.setattr(discovery, 'collect_official', missing)
    inputs, excluded = foreign.collect(tmp_path, ['AAPL US','4062 JP'])
    assert inputs == {'AAPL US':[], '4062 JP':[]}
    assert len(excluded)==2
    assert 'SEC_USER_AGENT' in next(e['reason'] for e in excluded if e['ticker']=='AAPL US')
    assert 'index changed' in next(e['reason'] for e in excluded if e['ticker']=='4062 JP')


def test_missing_selected_tickers_return_to_backlog():
    from bot.verified_research_batch import backlog
    assert backlog({'focus_tickers':['2330','2317'],'auto_research_batch':{
        'selected_tickers':['2330'],'no_result_tickers':['2330']}})==['2330','2317']


def test_help_does_not_need_lock_and_busy_run_returns_distinct_status(tmp_path):
    with exclusive_run(tmp_path):
        with pytest.raises(SystemExit) as exc:
            main(['--root',str(tmp_path),'--help'])
        assert exc.value.code == 0
        assert main(['--root',str(tmp_path),'--tickers','2330']) == 4


def test_discovery_rejects_future_release_before_model_input(monkeypatch):
    from bot import official_release_discovery as discovery, foreign_research_sources as foreign
    monkeypatch.setattr(discovery, 'PdfReader', lambda raw:SimpleNamespace(pages=[None]))
    monkeypatch.setattr(foreign, 'official_text', lambda *a,**kw:'August 4, 2050')
    def fetch(url, source):
        content=b'<a href="/ir/items/tannshinn2050Q1.pdf">new</a>' if source=='foreign_official_index' else b'%PDF test'
        return SimpleNamespace(document_id='fixture'),content
    with pytest.raises(ValueError,match='future'):
        discovery.collect_official('4062 JP',fetch,'2026-10-16')


def test_forced_refresh_never_falls_back_to_previous_success(tmp_path, monkeypatch):
    import requests
    from bot import foreign_research_sources as foreign
    monkeypatch.setenv('SEC_USER_AGENT','Research test@example.com')
    monkeypatch.setattr(foreign.time,'sleep',lambda _:None)
    calls=[];fail=[False]
    directory={'fields':['cik','name','ticker','exchange'],'data':[[1,'Issuer','AAA','Nasdaq']]}
    submissions={'cik':1,'tickers':['AAA'],'exchanges':['Nasdaq'],'filings':{'recent':{'accessionNumber':['a']}}}
    facts={'cik':1,'entityName':'Issuer','facts':{'us-gaap':{'Assets':{'units':{'USD':[
        {'val':100,'end':'2026-01-01','filed':'2026-02-01','accn':'a'}]}}}}}
    class Session:
        def mount(self,*args):pass
        def get(self,url,**kwargs):
            calls.append(url)
            if fail[0]:raise requests.ConnectionError('offline')
            value=facts if '/companyfacts/' in url else submissions if '/submissions/' in url else directory
            return SimpleNamespace(content=json.dumps(value).encode(),status_code=200,url=url,
                                   json=lambda:value,raise_for_status=lambda:None)
    monkeypatch.setattr(foreign.requests,'Session',Session)
    data,excluded=foreign.collect(tmp_path,['AAA US'])
    assert data['AAA US'] and not excluded and len(calls)==3
    data,excluded=foreign.collect(tmp_path,['AAA US'])
    assert data['AAA US'] and len(calls)==3
    fail[0]=True
    data,excluded=foreign.collect(tmp_path,['AAA US'],refresh=True)
    assert not data['AAA US'] and excluded and len(calls)==4
    data,excluded=foreign.collect(tmp_path,['AAA US'],cache_dir=tmp_path/'another-day')
    assert not data['AAA US'] and excluded and len(calls)==5


def test_long_official_rows_do_not_overwrite_each_others_evidence(tmp_path, monkeypatch):
    import copy
    from bot import foreign_research_sources as foreign, official_release_discovery as discovery
    with SourceRegistry(tmp_path) as registry:
        source=registry.capture_document('foreign_official_issuer',b'fixture',
            source_url='https://www.ibiden.com/report',suffix='html')
    entry={'format':'official_excerpt','source_document_id':source.document_id,'identity_source_id':source.document_id,
           'url':source.url,'row':{'financial_excerpt':'a'*700+'one'}}
    monkeypatch.setattr(discovery,'collect_official',lambda *a:copy.deepcopy(entry))
    monkeypatch.setattr(foreign,'validate_entry',lambda *a:None)
    first,_=foreign.collect(tmp_path,['4062 JP'])
    entry['row']['financial_excerpt']='a'*700+'two'
    second,_=foreign.collect(tmp_path,['4062 JP'])
    old_id=first['4062 JP'][0]['evidence_id'];new_id=second['4062 JP'][0]['evidence_id']
    assert old_id!=new_id
    with SourceRegistry(tmp_path) as registry:
        assert 'one' in registry.db.get_evidence(old_id).excerpt
        assert 'two' in registry.db.get_evidence(new_id).excerpt

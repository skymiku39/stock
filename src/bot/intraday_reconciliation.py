"""Reconcile archived SDK ticks, minute bars and official daily OHLC before replay.

This verifies the regular-lot session. Daily volume also includes other markets
(odd lots, fixed-price and block trades), so it is not equated to tick volume.
"""
from collections import defaultdict
from decimal import Decimal
import datetime as dt
import hashlib
import json
from pathlib import Path

import pandas as pd

from bot.source_registry import SourceRegistry, resolve_source_document_path
from bot.runtime_state import atomic_json


MARKET_FIELDS = ('open', 'high', 'low', 'close', 'volume', 'amount',
                 'bid_price', 'bid_volume', 'ask_price', 'ask_volume', 'tick_type')
VOLUME_REPORTS = {
    'intraday_odd': ('afterTrading/TWTC7U', '成交股數', 1),
    'after_hours_odd': ('afterTrading/TWT53U', '成交股數', 1),
    'fixed_price': ('afterTrading/BFT41U', '成交數量', 1000),
    'block': ('block/BFIAUU', '成交股數', 1),
}


def fetch_volume_components(db, root, day, *, session=None):
    """Retain the exchange's complete-market responses, including fixed-price ETFs."""
    import requests
    session = session or requests.Session()
    for name, (endpoint, column, multiplier) in VOLUME_REPORTS.items():
        url = f'https://www.twse.com.tw/rwd/zh/{endpoint}'
        params = dict(date=day.replace('-', ''), response='json')
        if name == 'fixed_price':
            params['selectType'] = 'ALLBUT0999'
        response = session.get(url, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        _validate_component(payload, day, column)
        SourceRegistry(root, db=db).register_document('twse_volume_'+name, response.content,
            kind='http_response', suffix='json', mime_type='application/json', url=response.url,
            as_of=day, metadata=dict(volume_column=column, share_multiplier=multiplier, market_scope='all'))


def _validate_component(payload, day, column):
    if (payload.get('stat') != 'OK' or payload.get('date') != day.replace('-', '')
            or not {'證券代號', column}.issubset(payload.get('fields', [])) or 'data' not in payload):
        raise ValueError('official market volume response missing or wrong date/fields')


def official_volume_components(db, root, symbol, day):
    parts = {}
    hashes = {}
    for name, (_, column, multiplier) in VOLUME_REPORTS.items():
        found = db.conn.execute("SELECT document_id FROM source_documents WHERE source=? AND as_of=? AND quality_status='ok' ORDER BY retrieved_at DESC LIMIT 1",
                                ('twse_volume_'+name, day)).fetchone()
        if not found:
            raise ValueError(f'official {name} volume missing; fetch exchange volume components first')
        source = db.get_source_document(found[0])
        raw = resolve_source_document_path(root, source).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source.content_hash:
            raise ValueError('official market volume archive hash mismatch')
        if json.loads(source.metadata_json).get('market_scope') != 'all':
            raise ValueError('official market volume report is filtered')
        payload = json.loads(raw)
        _validate_component(payload, day, column)
        records = [dict(zip(payload['fields'], row)) for row in payload['data']]
        parts[name] = sum(Decimal(row[column].replace(',', ''))*multiplier for row in records if row['證券代號'] == symbol)
        hashes[source.document_id] = source.content_hash
    return parts, hashes


def verified_archive_rows(db, root, symbol, day, interval):
    """Return canonical rows only after exact raw replay, including duplicate ns."""
    from bot.intraday_history import kbars_to_bars, ticks_to_bars
    actual = db.get_intraday_bars(symbol, interval=interval, start=day, end=day)
    if not actual or any(not b.source_record_id for b in actual):
        raise ValueError(f'{symbol} {day} {interval}: source-backed rows missing')
    expected = {}
    sources = {}
    for source_id in {b.source_record_id for b in actual}:
        source = db.get_source_document(source_id)
        if source is None:
            raise ValueError('intraday source manifest missing')
        content = resolve_source_document_path(root, source).read_bytes()
        if hashlib.sha256(content).hexdigest() != source.content_hash:
            raise ValueError('intraday raw archive hash mismatch')
        payload = json.loads(content)
        if (payload.get('schema_version') != 2 or payload.get('symbol') != symbol
                or payload.get('interval') != interval or 'sdk_response' not in payload):
            raise ValueError('intraday archive lacks original SDK columns')
        parser = ticks_to_bars if interval == 'tick' else kbars_to_bars
        for bar in parser(symbol, payload['sdk_response']):
            if bar.ts[:10] == day:
                expected[(source_id, bar.ts, bar.sequence)] = bar
        sources[source_id] = source
    if len(actual) != len(expected):
        raise ValueError('intraday raw/canonical row count mismatch')
    for bar in actual:
        raw = expected.get((bar.source_record_id, bar.ts, bar.sequence))
        if raw is None or any(getattr(raw, f) != getattr(bar, f) for f in MARKET_FIELDS):
            raise ValueError(f'intraday raw/canonical mismatch at {bar.ts} sequence {bar.sequence}')
    return actual, sources


def reconciled_day_available(db, root, symbol, day, interval='1m'):
    try:
        bars, sources = verified_archive_rows(db, root, symbol, day, interval)
        return all(b.quality_status == 'ok' for b in bars) and all(
            s.quality_status == 'ok' and json.loads(s.metadata_json).get('session_reconciled')
            for s in sources.values())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def reconcile_observed_prefix(db, root, symbol, day, *, observed_at):
    """Validate completed observed minutes; never label an ongoing day complete."""
    observed = pd.Timestamp(observed_at)
    if observed.tzinfo is None:
        raise ValueError('observed_at requires an explicit timezone')
    local = observed.tz_convert('Asia/Taipei').tz_localize(None)
    if str(local.date()) != day:
        raise ValueError('observed prefix is only for the current observed day')
    ticks, tick_sources = verified_archive_rows(db, root, symbol, day, 'tick')
    minutes, minute_sources = verified_archive_rows(db, root, symbol, day, '1m')
    session = [t for t in ticks if f'{day} 09:00:00' <= t.ts <= f'{day} 13:30:00.000000000']
    if not session or pd.Timestamp(session[-1].ts) > local:
        raise ValueError('no observed regular-session ticks or future timestamp')
    cutoff = min(local.floor('min'), pd.Timestamp(session[-1].ts).floor('min'),
                 max(pd.Timestamp(m.ts) for m in minutes), pd.Timestamp(f'{day} 13:30:00'))
    eligible = [t for t in session if pd.Timestamp(t.ts) < cutoff]
    completed = [m for m in minutes if pd.Timestamp(f'{day} 09:00:00') < pd.Timestamp(m.ts) <= cutoff]
    if not eligible or not completed:
        raise ValueError('no completed observed minute yet')
    grouped = defaultdict(list)
    for tick in eligible:
        bucket = (pd.Timestamp(tick.ts).floor('min') + pd.Timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')
        grouped[bucket].append(tick)
    if set(grouped) != {m.ts for m in completed}:
        raise ValueError('observed minute/tick coverage mismatch')
    for bar in completed:
        group = grouped[bar.ts]
        values = dict(open=group[0].close, high=max(t.close for t in group), low=min(t.close for t in group),
                      close=group[-1].close, volume=sum(Decimal(str(t.volume)) for t in group),
                      amount=sum(Decimal(str(t.close))*Decimal(str(t.volume))*1000 for t in group))
        if any(abs(Decimal(str(getattr(bar, key)))-Decimal(str(value))) > Decimal('.001') for key, value in values.items()):
            raise ValueError(f'observed minute/tick values mismatch at {bar.ts}')
    # A prefix receipt is attached to the replay; the unfinished full-day rows
    # and raw manifests deliberately keep their partial quality designation.
    receipt = dict(symbol=symbol, day=day, scope='completed_observed_minutes', session_complete=False,
        observed_at=str(observed), exclusive_tick_cutoff=str(cutoff), ticks=len(eligible), minutes=len(completed),
        numeric_fields_checked=6*len(completed), mismatches=0,
        source_hashes={k: s.content_hash for k, s in {**tick_sources, **minute_sources}.items()},
        limitation='Ticks and minute aggregates agree through this cutoff; future minutes and official full-day volume remain unverified.')
    return eligible, receipt


def reconcile_intraday_session(db, root, symbol, day, *, receipt=None):
    ticks, tick_sources = verified_archive_rows(db, root, symbol, day, 'tick')
    minutes, minute_sources = verified_archive_rows(db, root, symbol, day, '1m')
    def regular(bar):
        return f'{day} 09:00:00' <= bar.ts <= f'{day} 13:30:00.000000000'
    session = [b for b in ticks if regular(b)]
    minute_session = [b for b in minutes if regular(b)]
    if not session or not minute_session:
        raise ValueError('regular-lot session data missing')
    grouped = defaultdict(list)
    for tick in session:
        stamp = pd.Timestamp(tick.ts)
        # The closing auction belongs to the final minute, not a 13:31 bar.
        if stamp == pd.Timestamp(f'{day} 13:30:00'):
            stamp -= pd.Timedelta(nanoseconds=1)
        bucket = (stamp.floor('min') + pd.Timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')
        grouped[bucket].append(tick)
    if set(grouped) != {b.ts for b in minute_session}:
        raise ValueError('minute/tick timestamp coverage mismatch')
    checked = 0
    for bar in minute_session:
        group = grouped[bar.ts]
        expected = dict(open=group[0].close, high=max(t.close for t in group),
                        low=min(t.close for t in group), close=group[-1].close,
                        volume=sum(Decimal(str(t.volume)) for t in group),
                        amount=sum(Decimal(str(t.close))*Decimal(str(t.volume))*1000 for t in group))
        for field, value in expected.items():
            if abs(Decimal(str(getattr(bar, field))) - Decimal(str(value))) > Decimal('.001'):
                raise ValueError(f'minute/tick {field} mismatch at {bar.ts}')
            checked += 1
    daily = db.get_price_history(symbol, start=day, end=day)
    if len(daily) != 1 or daily[0].quality_status != 'ok' or not daily[0].source_record_id:
        raise ValueError('verified official daily OHLC missing')
    source = db.get_source_document(daily[0].source_record_id)
    if source is None or not source.source.startswith(('twse_', 'tpex_')):
        raise ValueError('official daily source missing')
    content = resolve_source_document_path(root, source).read_bytes()
    if hashlib.sha256(content).hexdigest() != source.content_hash:
        raise ValueError('official daily raw archive hash mismatch')
    if source.source != 'twse_stock_day':
        raise ValueError('official raw daily replay is currently supported for TWSE STOCK_DAY only')
    payload = json.loads(content)
    fields = payload.get('fields', [])
    required = {'日期', '開盤價', '最高價', '最低價', '收盤價', '成交股數'}
    if payload.get('stat') != 'OK' or not required.issubset(fields):
        raise ValueError('official daily response fields missing')
    date = dt.date.fromisoformat(day)
    raw_rows = [dict(zip(fields, row)) for row in payload['data']]
    matching = [row for row in raw_rows if row['日期'] == f'{date.year-1911}/{date.month:02d}/{date.day:02d}']
    if len(matching) != 1:
        raise ValueError('official daily period missing')
    raw = matching[0]
    for column, field in [('開盤價', 'open'), ('最高價', 'high'), ('最低價', 'low'), ('收盤價', 'close')]:
        if Decimal(raw[column].replace(',', '')) != Decimal(str(getattr(daily[0], field))):
            raise ValueError('official daily raw/canonical OHLC mismatch')
    if Decimal(raw['成交股數'].replace(',', '')) != Decimal(str(daily[0].volume))*1000:
        raise ValueError('official daily raw/canonical volume mismatch')
    ohlc = dict(open=session[0].close, high=max(t.close for t in session),
                low=min(t.close for t in session), close=session[-1].close)
    if any(getattr(daily[0], key) != value for key, value in ohlc.items()):
        raise ValueError('regular-lot/official daily OHLC mismatch')
    volume_parts, volume_hashes = official_volume_components(db, root, symbol, day)
    regular_volume = sum(Decimal(str(t.volume))*1000 for t in session)
    total = regular_volume + sum(volume_parts.values())
    if total != Decimal(raw['成交股數'].replace(',', '')):
        raise ValueError('regular-lot plus other exchange markets differs from official daily volume')
    result = dict(symbol=symbol, day=day, scope='regular_lot_session',
                  regular_ticks=len(session), minutes=len(minute_session),
                  numeric_fields_checked=checked + 4 + 5 + 1, mismatches=0,
                  volume_lots=sum(t.volume for t in session),
                  excluded_after_hours_ticks=len(ticks)-len(session),
                  source_hashes={**{k: s.content_hash for k, s in {**tick_sources, **minute_sources}.items()}, **volume_hashes},
                  daily_source_id=daily[0].source_record_id,
                  exchange_volume_parts_shares={k: str(v) for k, v in {'regular': regular_volume, **volume_parts}.items()},
                  official_daily_volume_shares=str(total),
                  limitation='Only regular-lot ticks are eligible for replay; other markets are used for daily-volume reconciliation.')
    with db.transaction() as conn:
        for interval, bars, sources in (('tick', ticks, tick_sources), ('1m', minutes, minute_sources)):
            conn.execute("UPDATE intraday_bars SET quality_status='ok' WHERE symbol=? AND interval=? AND ts>=? AND ts<=?",
                         (symbol, interval, f'{day} 09:00:00', f'{day} 13:30:00.000000000'))
            for source_id, source in sources.items():
                meta = json.loads(source.metadata_json)
                reconciled = dict(meta.get('regular_session_reconciliations', {}))
                reconciled[day] = result
                meta['regular_session_reconciliations'] = reconciled
                # A multi-day archive cannot be promoted by checking one day.
                complete = all(regular(b) for b in bars) and source.source_ref == f'{symbol}:{interval}:{day}:{day}'
                meta.update(regular_session_reconciled=True, session_reconciled=complete)
                conn.execute('UPDATE source_documents SET metadata_json=?, quality_status=? WHERE document_id=?',
                             (json.dumps(meta), 'ok' if complete else 'partial', source_id))
    if receipt is not None:
        atomic_json(Path(receipt), result)
    return result


def main(argv=None):
    import argparse
    from bot.stock_db import StockDB
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fetch-volume', action='store_true', help='Fetch official exchange volume components')
    args = parser.parse_args(argv)
    dt.date.fromisoformat(args.date)
    db = StockDB.open(root=args.root)
    try:
        if args.fetch_volume:
            fetch_volume_components(db, args.root, args.date)
        print(json.dumps(reconcile_intraday_session(db, args.root, args.symbol, args.date, receipt=args.output)))
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())

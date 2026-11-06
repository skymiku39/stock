"""Replay archived real quote callbacks with cumulative-volume gap detection."""
import datetime as dt
from bisect import bisect_right
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pandas as pd

from bot.source_registry import SourceRegistry
from bot.stock_db import IntradayBar


def read_observed_stream(db, root, path, symbol, day):
    raw = Path(path).read_bytes()
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    ticks, books, book_times, previous = [], [], [], None
    first_high = None
    for index, row in enumerate(rows):
        event = row['payload']
        if event['code'] != symbol:
            continue
        if any(event.get(k) is not False for k in ('simtrade', 'intraday_odd', 'suspend')):
            raise ValueError('stream must contain actual regular-lot nonsuspended events')
        when = pd.Timestamp(event['datetime'])
        received = pd.Timestamp(row['received_at'])
        if received.tzinfo is None or when.tzinfo is not None:
            raise ValueError('stream callback timestamps must have known timezone conventions')
        if (str(when.date()) != day or not dt.time(9) <= when.time() <= dt.time(13, 30)
                or when > received.tz_convert('Asia/Taipei').tz_localize(None)):
            raise ValueError('stream date/session/future timestamp mismatch')
        if row['kind'] == 'bidask':
            for side in ('bid', 'ask'):
                prices, volumes = event[side+'_price'], event[side+'_volume']
                if not prices or len(prices) != len(volumes):
                    raise ValueError('incomplete stream order book')
                for price, volume in zip(prices, volumes):
                    price, volume = Decimal(str(price)), Decimal(str(volume))
                    if (not price.is_finite() or price < 0 or not volume.is_finite()
                            or volume < 0 or volume != int(volume)):
                        raise ValueError('invalid stream order book price/volume')
            pos = bisect_right(book_times, when.value)
            book_times.insert(pos, when.value)
            books.insert(pos, event)
            continue
        if row['kind'] != 'tick':
            raise ValueError('unexpected stream event type')
        price, volume = Decimal(str(event['close'])), Decimal(str(event['volume']))
        amount = Decimal(str(event['amount']))
        if (not price.is_finite() or price <= 0 or volume <= 0 or volume != int(volume)
                or amount != price*volume*1000):
            raise ValueError('stream trade price/volume/amount mismatch')
        high, low = Decimal(str(event['high'])), Decimal(str(event['low']))
        total_volume, total_amount = Decimal(str(event['total_volume'])), Decimal(str(event['total_amount']))
        if not low <= price <= high or total_volume < volume or total_amount < amount:
            raise ValueError('invalid cumulative market values')
        if previous:
            prior_time, prior_event = previous
            if when < prior_time:
                raise ValueError('out-of-order stream ticks')
            if (total_volume-Decimal(str(prior_event['total_volume'])) != volume
                    or total_amount-Decimal(str(prior_event['total_amount'])) != amount):
                raise ValueError('stream cumulative totals reveal missing or duplicate trades')
            if high < Decimal(str(prior_event['high'])) or low > Decimal(str(prior_event['low'])):
                raise ValueError('stream day high/low moved backwards')
        else:
            first_high = float(high)
        previous = (when, event)
        # Never attach a book from the future, or a quote older than one second.
        pos = bisect_right(book_times, when.value)-1
        book = books[pos] if pos >= 0 and when.value-book_times[pos] <= 1_000_000_000 else None
        ticks.append(IntradayBar(symbol=symbol, ts=str(when), interval='tick', sequence=index,
            open=float(price), high=float(price), low=float(price), close=float(price), volume=int(volume),
            amount=float(amount), tick_type=int(event['tick_type']), quality_status='partial',
            bid_price=float(book['bid_price'][0]) if book else None,
            ask_price=float(book['ask_price'][0]) if book else None,
            bid_volume=int(book['bid_volume'][0]) if book else None,
            ask_volume=int(book['ask_volume'][0]) if book else None))
    if len(ticks) < 2:
        raise ValueError('at least two real ticks are required for cumulative reconciliation')
    receipt = dict(symbol=symbol, day=day, scope='observed_stream_interval', session_complete=False,
        first_tick=ticks[0].ts, last_tick=ticks[-1].ts, ticks=len(ticks),
        first_day_high=first_high, last_day_high=float(previous[1]['high']),
        cumulative_volume_amount_checks=2*(len(ticks)-1), mismatches=0,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        limitation='Only the captured interval is reconciled; full-day exchange totals remain unverified.')
    source = SourceRegistry(root, db=db).register_document('shioaji_stream', raw, kind='sdk_response',
        suffix='jsonl', mime_type='application/x-ndjson', as_of=day, quality_status='partial',
        source_ref=f'{symbol}:{ticks[0].ts}:{ticks[-1].ts}', metadata=receipt)
    for tick in ticks:
        tick.source_record_id = source.document_id
    receipt['source_document_id'] = source.document_id
    return ticks, receipt


def capture_stream(root, output, symbols, seconds=120):
    """Bounded, quote-only subscription; SDK errors never persist session tokens."""
    import contextlib
    import io
    import threading
    import time
    from zoneinfo import ZoneInfo
    from bot.config import Settings
    from bot.runtime_state import atomic_json
    import shioaji as sj

    if not 1 <= seconds <= 16200 or not symbols:
        raise ValueError('provide symbols and a capture duration of 1..16200 seconds')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    settings = Settings(_env_file=str(Path(root)/'.env'), simulation=True, run_mode='watch')
    counts, errors, guard = {}, [], threading.Lock()
    api = None

    def save(kind, event):
        try:
            payload = event.to_dict() if hasattr(event, 'to_dict') else event.dict()
            row = dict(kind=kind, received_at=dt.datetime.now(ZoneInfo('Asia/Taipei')).isoformat(), payload=payload)
            with guard:
                with (output/'events.jsonl').open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(row, default=str)+'\n')
                key = str(event.code)+':'+kind
                counts[key] = counts.get(key, 0)+1
        except Exception as exc:
            errors.append(type(exc).__name__)

    # SDK timeout strings can contain authentication payloads. Retain only
    # exception types and public quote callbacks, never str(exc) or SDK logs.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            api = sj.Shioaji(simulation=True)
            api.login(api_key=settings.api_key, secret_key=settings.secret_key, contracts_timeout=60000)
            api.on_tick_stk_v1()(lambda exchange, event: save('tick', event))
            api.on_bidask_stk_v1()(lambda exchange, event: save('bidask', event))
            for symbol in symbols:
                for kind in (sj.constant.QuoteType.Tick, sj.constant.QuoteType.BidAsk):
                    api.quote.subscribe(api.Contracts.Stocks[symbol], quote_type=kind, version=sj.constant.QuoteVersion.v1)
            deadline = time.monotonic()+seconds
            while time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline-time.monotonic())))
                with guard:
                    atomic_json(output/'status.json', dict(counts=counts, errors=errors,
                        observed_at=dt.datetime.now(ZoneInfo('Asia/Taipei')).isoformat(), external_orders=0))
        except Exception as exc:
            atomic_json(output/'failure.json', dict(error_type=type(exc).__name__))
            raise RuntimeError('quote capture failed: '+type(exc).__name__) from None
        finally:
            if api:
                try:
                    api.logout()
                except Exception as exc:
                    errors.append('logout:'+type(exc).__name__)
    result = dict(counts=counts, errors=errors, simulation_login=True, external_orders=0)
    atomic_json(output/'capture.json', result)
    if errors or any(counts.get(symbol+':tick', 0) < 2 for symbol in symbols):
        raise ValueError('quote capture incomplete; inspect capture.json')
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--symbols', nargs='+', required=True)
    parser.add_argument('--seconds', type=int, default=120)
    args = parser.parse_args(argv)
    print(json.dumps(capture_stream(args.root, args.output, args.symbols, args.seconds)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Read Yuanta's published Nuxt data as literals, never execute page scripts."""
from __future__ import annotations

import json
import re
from datetime import datetime

from bs4 import BeautifulSoup


class _Literals:
    def __init__(self, text, names=None):
        self.text, self.pos, self.names = text, 0, names or {}

    def ws(self):
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def take(self, char):
        self.ws()
        if not self.text.startswith(char, self.pos):
            raise ValueError('unsupported serialized fund data')
        self.pos += len(char)

    def value(self, depth=0):
        self.ws()
        if depth > 80 or self.pos >= len(self.text):
            raise ValueError('invalid serialized fund data')
        char = self.text[self.pos]
        if char == '"':
            value, consumed = json.JSONDecoder().raw_decode(self.text[self.pos:])
            self.pos += consumed
            return value
        if char in '[{':
            self.pos += 1
            closing = ']' if char == '[' else '}'
            result = [] if char == '[' else {}
            self.ws()
            while self.text[self.pos:self.pos+1] != closing:
                if isinstance(result, dict):
                    self.ws()
                    if self.text[self.pos:self.pos+1] == '"':
                        key = self.value(depth+1)
                    else:
                        match = re.match(r'[A-Za-z_$][\w$]*', self.text[self.pos:])
                        if not match:
                            raise ValueError('invalid fund data key')
                        key = match[0]
                        self.pos += len(key)
                    if key in result:
                        raise ValueError('duplicate fund data key')
                    self.take(':')
                    result[key] = self.value(depth+1)
                else:
                    result.append(self.value(depth+1))
                self.ws()
                if self.text[self.pos:self.pos+1] == closing:
                    break
                self.take(',')
            self.take(closing)
            return result
        match = re.match(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', self.text[self.pos:])
        if match:
            self.pos += len(match[0])
            return float(match[0]) if any(c in match[0] for c in '.eE') else int(match[0])
        match = re.match(r'Array\((\d+)\)', self.text[self.pos:])
        if match and int(match[1]) <= 10000:
            self.pos += len(match[0])
            return [None] * int(match[1])
        match = re.match(r'[A-Za-z_$][\w$]*', self.text[self.pos:])
        if match:
            self.pos += len(match[0])
            names = {'true': True, 'false': False, 'null': None, **self.names}
            if match[0] in names:
                return names[match[0]]
        raise ValueError('non-literal expression in fund data')

    def parse(self):
        value = self.value()
        self.ws()
        if self.pos != len(self.text):
            raise ValueError('unexpected expression after fund data')
        return value


def nuxt_state(raw):
    soup = BeautifulSoup(raw.decode('utf-8'), 'html.parser')
    scripts = [s.get_text() for s in soup.find_all('script') if 'window.__NUXT__=' in s.get_text()]
    if len(scripts) != 1:
        raise ValueError('missing or ambiguous fund data state')
    match = re.fullmatch(r'\s*window\.__NUXT__=\(function\(([^)]*)\)\{(.*?)return (.*)\}\((.*)\)\);?\s*', scripts[0], re.S)
    if not match:
        raise ValueError('unsupported fund data serialization')
    args = _Literals('[' + match[4] + ']').parse()
    names = match[1].split(',')
    if len(names) != len(args) or len(set(names)) != len(names):
        raise ValueError('invalid fund data bindings')
    # Nuxt emits assignments for shared menu objects before `return`. Ignore
    # those objects, and reject references to them in the selected fund data.
    mutated = set(re.findall(r'\b([A-Za-z_$][\w$]*)[.\[]', match[2]))
    bindings = {k:v for k,v in zip(names,args) if k not in mutated}
    sections = {}
    for label in ('fundData', 'weightData'):
        positions = list(re.finditer(r'\b' + label + ':', match[3]))
        if len(positions) != 1:
            raise ValueError('missing or ambiguous selected fund section')
        sections[label] = _Literals(match[3][positions[0].end():], bindings).value()
    return sections


def parse_snapshot(raw, ticker):
    state = nuxt_state(raw)
    fund, portfolio = state['fundData'], state['weightData']
    pcf, weights = portfolio['PCF'], portfolio['FundWeights']
    if (fund['STK_CD'] != ticker or pcf['markcd'] != ticker
            or fund['FUND_ID'] != pcf['fundid'] or weights['Summary']['code'] != pcf['fundid']):
        raise ValueError('fund identity mismatch')
    day = datetime.strptime(str(pcf['trandate']), '%Y%m%d').date().isoformat()
    from bot.research_analysis import number
    nav = number(pcf['nav'])
    if nav is None or nav <= 0:
        raise ValueError('missing fund NAV')
    holdings = weights['StockWeights']
    codes = set()
    for h in holdings:
        weight = number(h['weights'])
        if not h['code'] or h['code'] in codes or weight is None or not 0 <= weight <= 100:
            raise ValueError('invalid or duplicate fund holding')
        codes.add(h['code'])
    if not holdings or sum(float(h['weights']) for h in holdings) > 101:
        raise ValueError('invalid stock holdings coverage')
    return {
        'holdings': {'ticker':ticker, 'as_of':day, 'holdings':holdings,
                     'coverage':'published_stock_holdings', 'count':len(holdings)},
        'nav': {'ticker':ticker, 'as_of':day, 'nav':pcf['nav'], 'currency':'TWD',
                'net_assets':pcf['totalav'], 'outstanding_units':pcf['osunit'], 'basis':'official_end_of_day'},
    }

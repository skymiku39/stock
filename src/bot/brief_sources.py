"""Rebuild brief facts from pinned raw sources; never trust derived summaries."""

import datetime as dt
import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path

from bot.active_etf import HoldingsSnapshot
from bot.evidence import make_evidence
from bot.source_registry import resolve_source_document_path


class BriefSources:
    def __init__(self, root, db):
        self.root, self.db = Path(root), db
        self.packets = {}
        self.excluded = []
        self._raw = {}

    def omit(self, component, reason):
        item = {"component": component, "reason": reason}
        if item not in self.excluded:
            self.excluded.append(item)

    def raw(self, source_id):
        if source_id in self._raw:
            return self._raw[source_id]
        source = self.db.get_source_document(source_id)
        if source is None or source.quality_status not in {"ok", "partial"}:
            raise ValueError("missing or unusable source")
        raw = resolve_source_document_path(self.root, source).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source.content_hash:
            raise ValueError("raw hash mismatch")
        self._raw[source_id] = source, raw
        return source, raw

    def evidence(self, source, *, as_of, facts):
        # Hash the complete replayed facts, not the truncated display excerpt.
        encoded = json.dumps(facts, ensure_ascii=False, sort_keys=True, allow_nan=False)
        ref = source.document_id + ":" + hashlib.sha256(encoded.encode()).hexdigest()
        packet = make_evidence(
            "brief_source", source=source.source, source_ref=ref, url=source.url,
            title="Replayed brief input", excerpt=encoded, as_of=as_of,
            retrieved_at=source.retrieved_at, source_document_id=source.document_id,
            quality_status=source.quality_status, provenance_status="exact",
            metadata={"replayed_facts_sha256": hashlib.sha256(encoded.encode()).hexdigest()},
        )
        self.packets[packet["id"]] = packet
        return packet["id"]

    def macro(self, snapshot):
        if not snapshot:
            return {}
        try:
            source, content = self.raw(str(snapshot.get("source_document_id") or ""))
            if source.source != "market_macro":
                raise ValueError("wrong macro source type")
            raw = json.loads(content)
            for key in ("indices", "stocks", "adr_premiums"):
                if not same_json_value(raw.get(key), snapshot.get(key)):
                    raise ValueError(f"{key} differs from raw")
            result = {"indices": {}, "stocks": {}, "adr_premiums": []}
            for group in ("indices", "stocks"):
                for symbol, quote in raw.get(group, {}).items():
                    date = str(quote.get("source_date") or "")
                    price_key = "price"
                    if not valid_date(date) or not positive(quote.get(price_key)):
                        self.omit(f"macro:{symbol}", "missing source date or valid price")
                        continue
                    # Role / Taiwan mapping comes from editable configuration.
                    result[group][symbol] = {k: v for k, v in quote.items()
                        if k in {"symbol", "name", "price", "prev_close", "volume", "market_cap", "source_date", "source"}
                        and not (isinstance(v, float) and not math.isfinite(v))}
                    if positive(quote.get("prev_close")):
                        result[group][symbol]["pct_change"] = (quote["price"] / quote["prev_close"] - 1) * 100
            for adr in raw.get("adr_premiums", []):
                dates = [adr.get(k) for k in ("adr_source_date", "tw_source_date", "fx_source_date")]
                if not all(valid_date(d) for d in dates) or len(set(dates)) != 1:
                    self.omit(f"adr:{adr.get('adr_symbol')}", "unaligned or unknown source dates")
                    continue
                # The current collector's ratios are configuration constants;
                # arithmetic alone cannot verify the ADR/share relationship.
                self.omit(f"adr:{adr.get('adr_symbol')}", "ADR conversion ratio has no independent source identity")
                continue
            if raw.get("futures_basis"):
                futures = self.futures(raw["futures_basis"], result["indices"].get("^TWII", {}))
                if futures:
                    result["futures_basis"] = futures
            if any(result.values()):
                result["evidence_id"] = self.evidence(source, as_of=source.as_of, facts=result)
                return result
        except (ValueError, OSError, TypeError, KeyError) as exc:
            self.omit("macro", str(exc))
        return {}

    def futures(self, basis, spot):
        from bot.market_macro import _taifex_rows_from_bytes, _front_tx_row, _build_futures_basis
        try:
            source, raw = self.raw(str(basis.get("source_document_id") or ""))
            if source.source != "taifex_daily_fut":
                raise ValueError("wrong futures source type")
            front = _front_tx_row(_taifex_rows_from_bytes(raw))
            if not front:
                raise ValueError("missing front-month contract")
            date = str(front.get("Date") or "")
            if len(date) == 8 and date.isdigit():
                date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
            if not valid_date(date) or date != spot.get("source_date") or not positive(spot.get("price")):
                raise ValueError("spot and futures source dates differ or are unknown")
            replay = _build_futures_basis(front, spot["price"], source_document_id=source.document_id)
            if replay is None:
                raise ValueError("invalid futures price")
            facts = asdict(replay)
            facts["asof_date"] = date
            facts["evidence_id"] = self.evidence(source, as_of=date, facts=facts)
            return facts
        except (ValueError, OSError, TypeError, KeyError) as exc:
            self.omit("futures_basis", str(exc))
            return {}

    def holdings(self, snapshots):
        from bot.etf_holdings_fetcher import _parse_etfinfo_holdings_html
        from bot.fund_holdings_parser import parse_moneydj_fund_holdings_html
        from bot.etf_holdings_validator import validate_etf_holdings

        verified, evidence = {}, {}
        for symbol, snapshot in snapshots.items():
            target = sorted((asdict(h) for h in snapshot.holdings), key=lambda h: h["ticker"])
            candidates = self.db.conn.execute(
                "SELECT source_document_id, canonical_text FROM research_documents "
                "WHERE document_type='etf_holdings' AND symbol=? AND status='active'",
                (symbol,),
            ).fetchall()
            seen = set()
            for candidate in candidates:
                source_id = candidate["source_document_id"]
                if source_id in seen:
                    continue
                try:
                    canonical = json.loads(candidate["canonical_text"])
                    if canonical.get("as_of") != snapshot.date.isoformat() or sorted(canonical.get("holdings", []), key=lambda h: h["ticker"]) != target:
                        continue
                    seen.add(source_id)
                    source, content = self.raw(source_id)
                    if source.source != f"etf_holdings_{symbol}":
                        continue
                    html = content.decode("utf-8")
                    if "etfinfo.tw" in source.url:
                        parsed = _parse_etfinfo_holdings_html(html, symbol=symbol)
                        date, rows = parsed if parsed else (None, [])
                    else:
                        # No live company-map lookups or guessed ticker identities.
                        parsed = parse_moneydj_fund_holdings_html(html, root=self.root, company_map={})
                        date, rows = parsed.as_of, parsed.holdings
                    if date != snapshot.date or sorted((asdict(h) for h in rows), key=lambda h: h["ticker"]) != target:
                        continue
                    if not validate_etf_holdings(snapshot, as_of=date.isoformat(),
                            retrieved_at=source.retrieved_at, source_quality_status=source.quality_status).usable:
                        continue
                    verified[symbol] = HoldingsSnapshot(symbol, date, rows)
                    evidence[symbol] = self.evidence(source, as_of=date.isoformat(), facts=target)
                    break
                except (ValueError, OSError, TypeError, KeyError):
                    continue
            if symbol not in verified:
                self.omit(f"holdings:{symbol}:{snapshot.date}", "no exact canonical/raw replay")
        return verified, evidence

    def chips(self, summaries):
        from bot.chips_fetcher import _parse_t86, _parse_tpex_insti, _parse_margin, _parse_tpex_margin, _parse_borrow

        sources = sorted({doc for row in summaries for doc in row.get("source_document_ids", [])})
        tickers = {row["ticker"] for row in summaries}
        observations = {}
        for doc in sources:
            try:
                source, content = self.raw(doc)
                raw = json.loads(content)
                date = raw.get("date", "")
                if len(date) == 8 and date.isdigit():
                    date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
                if not valid_date(date) or date != source.as_of:
                    raise ValueError("response date differs from manifest")
                name = source.source
                if name == "chips_twse_qfii_daily":
                    parsed = {t: {k: getattr(r, k) for k in r.available_fields} for t, r in _parse_t86(raw).items()}
                elif name == "chips_tpex_tpex_insti":
                    parsed = {t: {k: getattr(r, k) for k in r.available_fields} for t, r in _parse_tpex_insti(raw["tables"][0]).items()}
                elif name in {"chips_twse_margin_balance", "chips_tpex_tpex_margin"}:
                    values = _parse_margin(raw) if name.startswith("chips_twse") else _parse_tpex_margin(raw["tables"][0])
                    parsed = {t: {"margin_balance": v[0], "short_balance": v[1]} for t, v in values.items()}
                elif name == "chips_twse_borrow_balance":
                    parsed = {t: {"borrow_balance": v} for t, v in _parse_borrow(raw).items()}
                else:
                    continue  # Block volume is not directional net buying.
                selected = {t: v for t, v in parsed.items() if t in tickers}
                if not selected:
                    continue
                eid = self.evidence(source, as_of=date, facts=selected)
                for ticker, values in selected.items():
                    for metric, value in values.items():
                        key = (ticker, date, metric)
                        observations.setdefault(key, []).append((value, eid))
            except (ValueError, OSError, TypeError, KeyError, IndexError) as exc:
                self.omit(f"chips:{doc}", str(exc))
        output = {}
        for (ticker, date, metric), values in sorted(observations.items()):
            if len({v for v, _ in values}) != 1 or not math.isfinite(values[0][0]):
                self.omit(f"chips:{ticker}:{date}:{metric}", "conflicting/nonfinite raw values")
                continue
            daily = output.setdefault(ticker, {}).setdefault(date, {"date": date, "values": {}, "evidence_ids": []})
            daily["values"][metric] = values[0][0]
            daily["evidence_ids"] = sorted(set(daily["evidence_ids"]) | {e for _, e in values})
        return [{"ticker": t, "observations": list(rows.values()), "units": "張; missing metrics are unavailable"}
                for t, rows in sorted(output.items())]


def valid_date(value):
    try:
        return bool(value) and dt.date.fromisoformat(value).isoformat() == value
    except (ValueError, TypeError):
        return False


def positive(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def same_json_value(left, right):
    """Compare raw JSON including unavailable NaN values, never as valid facts.

    Independent JSON decodes produce unequal NaNs. Compare their serialized
    representation before the field-level finite-value gate excludes them.
    """
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)

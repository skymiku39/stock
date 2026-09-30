"""fixture-driven ETF holdings domain validator tests."""

from __future__ import annotations

import json
from pathlib import Path

from bot.active_etf import Holding, HoldingsSnapshot
from bot.data_quality import QUALITY_INVALID, QUALITY_OK, QUALITY_PARTIAL
from bot.etf_holdings_validator import (
    validate_etf_holdings,
    validate_etf_holdings_rows,
)
import datetime as dt


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "etf_holdings"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_fixture_valid_near_100_is_ok() -> None:
    payload = _load("valid_near_100.json")
    result = validate_etf_holdings(
        payload,
        as_of=payload["as_of"],
        retrieved_at="2026-09-20T08:00:00+08:00",
        source_quality_status="ok",
    )
    assert result.usable
    assert result.quality_status == QUALITY_OK
    assert not result.errors
    assert abs(result.weight_sum - 100.0) < 0.01


def test_fixture_duplicate_ticker_is_invalid() -> None:
    payload = _load("duplicate_ticker.json")
    result = validate_etf_holdings(payload, as_of=payload["as_of"])
    assert result.quality_status == QUALITY_INVALID
    assert result.duplicate_tickers == ["2330"]
    assert any("重複" in message for message in result.errors)


def test_fixture_negative_weight_is_invalid() -> None:
    payload = _load("negative_weight.json")
    result = validate_etf_holdings_rows(
        payload["holdings"],
        symbol=payload["symbol"],
        as_of=payload["as_of"],
    )
    assert result.quality_status == QUALITY_INVALID
    assert any("負" in message for message in result.errors)


def test_fixture_weight_sum_warning_is_partial() -> None:
    payload = _load("weight_sum_warning.json")
    result = validate_etf_holdings(payload, as_of=payload["as_of"])
    assert result.quality_status == QUALITY_PARTIAL
    assert result.usable
    assert any("權重加總" in message for message in result.warnings)


def test_as_of_must_not_equal_retrieved_at_timestamp() -> None:
    snapshot = HoldingsSnapshot(
        symbol="00980A",
        date=dt.date(2026, 9, 19),
        holdings=[Holding(ticker="2330", name="TSMC", weight_pct=100.0)],
    )
    result = validate_etf_holdings(
        snapshot,
        as_of="2026-09-20T08:00:00+08:00",
        retrieved_at="2026-09-20T08:00:00+08:00",
    )
    assert result.quality_status == QUALITY_INVALID
    assert any("as_of" in message for message in result.errors)


def test_unknown_source_is_not_auto_upgraded_to_ok() -> None:
    payload = _load("valid_near_100.json")
    result = validate_etf_holdings(
        payload,
        as_of=payload["as_of"],
        retrieved_at="2026-09-20T08:00:00+08:00",
        source_quality_status="unknown",
    )
    assert result.quality_status == QUALITY_PARTIAL
    assert result.usable



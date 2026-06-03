"""SjBroker safety checks for quote-only modes."""

from __future__ import annotations

import bot.broker as broker_module
from bot.broker import SjBroker
from bot.config import Settings
from shioaji.constant import Action, OrderType, QuoteType, StockOrderCond, StockPriceType


def _settings(**overrides) -> Settings:
    defaults = dict(
        symbols=["2330"],
        api_key="key",
        secret_key="secret",
        ca_path="C:/certs/Sinopac.pfx",
        ca_password="pw",
        person_id="TEST_PERSON_ID",
        simulation=False,
        _env_file=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


def test_trade_real_mode_can_activate_ca() -> None:
    broker = SjBroker(_settings(run_mode="trade"))
    assert broker._should_activate_ca() is True


def test_watch_mode_never_activates_ca() -> None:
    broker = SjBroker(_settings(run_mode="watch"))
    assert broker._should_activate_ca() is False


def test_simulation_trade_does_not_activate_ca() -> None:
    broker = SjBroker(_settings(run_mode="trade", simulation=True))
    assert broker._should_activate_ca() is False


def test_custom_field_is_sanitized_to_shioaji_limit() -> None:
    assert broker_module._clean_custom_field("smoke-test-123") == "smoket"
    assert broker_module._clean_custom_field("測試!@ab_cd") == "abcd"


def test_quote_type_label_for_newer_shioaji_subscribe_api() -> None:
    assert broker_module._quote_type_label(QuoteType.Tick) == "tick"
    assert broker_module._quote_type_label(QuoteType.BidAsk) == "bidask"


def test_build_stock_order_uses_cash_common_order(monkeypatch) -> None:
    captured = {}

    class FakeApi:
        stock_account = object()

        @staticmethod
        def Order(**kwargs):
            captured.update(kwargs)
            return kwargs

    monkeypatch.setattr(broker_module.sj, "StockOrder", None, raising=False)
    broker = SjBroker(_settings(run_mode="trade", simulation=True))
    broker.api = FakeApi()  # type: ignore[assignment]

    order = broker._build_stock_order(
        price=10.0,
        quantity=1,
        action=Action.Buy,
        price_type=StockPriceType.LMT,
        order_type=OrderType.ROD,
        custom_field="smoke-test",
    )

    assert order == captured
    assert captured["order_lot"].value == "Common"
    assert captured["order_cond"] == StockOrderCond.Cash
    assert captured["custom_field"] == "smoket"


def test_build_stock_order_supports_intraday_odd_lot(monkeypatch) -> None:
    from shioaji.constant import StockOrderLot

    captured = {}

    class FakeApi:
        stock_account = object()

        @staticmethod
        def Order(**kwargs):
            captured.update(kwargs)
            return kwargs

    monkeypatch.setattr(broker_module.sj, "StockOrder", None, raising=False)
    broker = SjBroker(_settings(run_mode="trade", simulation=True))
    broker.api = FakeApi()  # type: ignore[assignment]

    broker._build_stock_order(
        price=600.0,
        quantity=10,
        action=Action.Buy,
        price_type=StockPriceType.LMT,
        order_type=OrderType.ROD,
        custom_field="oddbuy",
        order_lot=StockOrderLot.IntradayOdd,
    )

    assert captured["order_lot"] == StockOrderLot.IntradayOdd
    assert captured["quantity"] == 10


def test_lookup_stock_contract_falls_back_to_market_namespace() -> None:
    contract = object()

    class MarketContracts:
        @staticmethod
        def get(symbol):
            return contract if symbol == "2330" else None

    class Stocks:
        TSE = MarketContracts()
        OTC = MarketContracts()

        @staticmethod
        def get(symbol):
            return None

    class Contracts:
        pass

    Contracts.Stocks = Stocks()

    class FakeApi:
        pass

    FakeApi.Contracts = Contracts()

    assert broker_module._lookup_stock_contract(FakeApi(), "2330") is contract

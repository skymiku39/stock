from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from shioaji.constant import Action, OrderState, OrderType, StockOrderLot, StockPriceType

from bot.config import Settings
from bot.t4_broker import (
    T4Broker,
    map_order_type,
    map_stock_ord_type,
    map_stock_price_type,
    parse_accounts,
    parse_order_response,
    parse_stock_reply,
)


def _settings(**overrides) -> Settings:
    defaults = dict(
        run_mode="trade",
        symbols=["2330"],
        broker_backend="t4",
        t4_login_id="A123456789",
        t4_login_password="pw",
        t4_person_id="A123456789",
        t4_ca_path="C:/ekey/Sinopac.pfx",
        t4_ca_password="capw",
        simulation=False,
        _env_file=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


def _field(value: str, width: int) -> str:
    return str(value)[:width].ljust(width)


def _stock_reply(**overrides: str) -> str:
    values = {
        "trade_type": "01",
        "account": "1234567",
        "code": "2890",
        "place_price": "13.00",
        "volume": "1",
        "ord_seq": "448093",
        "ord_date": "20260602",
        "effective_date": "20260602",
        "time": "093152",
        "ord_no": "IJ904",
        "web_id": "152",
        "org_ord_seq": "",
        "ord_bs": "B",
        "ord_type": "0",
        "place_type": "0",
        "market_id": "S",
        "price_type": " ",
        "status": "00",
        "err": "order ok",
        "mprice_flag": "2",
        "ordknd": "0",
    }
    values.update(overrides)
    parts = [
        _field(values["trade_type"], 2),
        _field(values["account"], 15),
        _field(values["code"], 6),
        _field(values["place_price"], 9),
        _field(values["volume"], 6),
        _field(values["ord_seq"], 6),
        _field(values["ord_date"], 8),
        _field(values["effective_date"], 8),
        _field(values["time"], 6),
        _field(values["ord_no"], 5),
        _field(values["web_id"], 3),
        _field(values["org_ord_seq"], 6),
        _field(values["ord_bs"], 1),
        _field(values["ord_type"], 1),
        _field(values["place_type"], 1),
        _field(values["market_id"], 1),
        _field(values["price_type"], 1),
        _field(values["status"], 2),
        _field(values["err"], 60),
        _field(values["mprice_flag"], 1),
        _field(values["ordknd"], 1),
    ]
    reply = "".join(parts)
    assert len(reply) == 149
    return reply


def _order_response(**overrides: str) -> str:
    values = {
        "seqn": "00542731",
        "branch": "S9A95",
        "account": "1234567",
        "ord_no": "IJ904",
        "ord_seq": "448093",
        "code": "2890",
        "ord_type": "01",
        "ord_class": "02",
        "place_price": "13.00",
        "matched_price": "13.10",
        "ordknd": "ROD",
        "volume": "1",
        "time": "112459",
        "status": "order ok",
        "ecode": "00",
        "err": "order ok",
        "web_id": "152",
        "account_s": "",
        "oct": "0",
        "ord_time": "112459",
        "agent_id": "",
        "price_type": " ",
        "tr_fld": "",
        "matched_seqn": "",
        "func_seqn": "",
        "mprice_flag": "2",
    }
    values.update(overrides)
    body = "".join([
        _field(values["seqn"], 8),
        _field(values["branch"], 8),
        _field(values["account"], 7),
        _field(values["ord_no"], 5),
        _field(values["ord_seq"], 6),
        _field(values["code"], 10),
        _field(values["ord_type"], 3),
        _field(values["ord_class"], 2),
        _field(values["place_price"], 10),
        _field(values["matched_price"], 10),
        _field(values["ordknd"], 3),
        _field(values["volume"], 6),
        _field(values["time"], 6),
        _field(values["status"], 20),
        _field(values["ecode"], 4),
        _field(values["err"], 60),
        _field(values["web_id"], 3),
        _field(values["account_s"], 15),
        _field(values["oct"], 1),
        _field(values["ord_time"], 6),
        _field(values["agent_id"], 6),
        _field(values["price_type"], 1),
        _field(values["tr_fld"], 4),
        _field(values["matched_seqn"], 8),
        _field(values["func_seqn"], 6),
        _field(values["mprice_flag"], 1),
    ])
    assert len(body) == 219
    return "0001" + body


@dataclass
class FakeT4Dll:
    reply: str = field(default_factory=_stock_reply)
    response_queue: List[str] = field(default_factory=list)
    calls: List[Tuple[str, tuple]] = field(default_factory=list)

    def init_t4(self, login_id: str, login_pass: str, dll_path: str = "") -> str:
        self.calls.append(("init_t4", (login_id, login_pass, dll_path)))
        return "init ok"

    def log_out(self) -> int:
        self.calls.append(("log_out", ()))
        return 0

    def show_version(self) -> str:
        return "1.0.14.2"

    def show_list2(self) -> str:
        return "S9A95-1234567-Stock User\nF002000-7654321-Fut User\n"

    def add_acc_ca(self, branch: str, account: str, acc_id: str, acc_ca_path: str, acc_ca_pass: str) -> str:
        self.calls.append(("add_acc_ca", (branch, account, acc_id, acc_ca_path, acc_ca_pass)))
        return "ca ok"

    def verify_ca_pass(self, branch: str, account: str) -> str:
        self.calls.append(("verify_ca_pass", (branch, account)))
        return ""

    def stock_order2(
        self,
        buy_sell: str,
        branch: str,
        account: str,
        code: str,
        ord_type: str,
        price: str,
        amount: str,
        price_type: str,
        ord_knd: str,
    ) -> str:
        self.calls.append((
            "stock_order2",
            (buy_sell, branch, account, code, ord_type, price, amount, price_type, ord_knd),
        ))
        return self.reply

    def stock_cancel(self, *args: str) -> str:
        self.calls.append(("stock_cancel", args))
        return ""

    def check_response_buffer(self) -> int:
        return len(self.response_queue)

    def timer_response_log(self) -> str:
        return self.response_queue.pop(0)

    def do_register(self, yes_no: int) -> int:
        self.calls.append(("do_register", (yes_no,)))
        return 0


def test_parse_accounts_splits_market_prefix() -> None:
    accounts = parse_accounts("S9A95-1234567-Alice\nF002000-7654321-Bob\n")

    assert len(accounts) == 2
    assert accounts[0].is_stock
    assert accounts[0].branch_with_market == "S9A95"
    assert accounts[0].branch == "9A95"
    assert accounts[0].account == "1234567"
    assert accounts[1].is_future_option


def test_mapping_matches_t4_stock_values() -> None:
    assert map_stock_ord_type(StockOrderLot.Common) == "00"
    assert map_stock_ord_type(StockOrderLot.IntradayOdd) == "C0"
    assert map_stock_price_type(StockPriceType.LMT) == " "
    assert map_stock_price_type(StockPriceType.MKT) == "1"
    assert map_order_type(OrderType.IOC) == "IOC"


def test_parse_stock_reply_fixed_width() -> None:
    parsed = parse_stock_reply(_stock_reply())

    assert parsed.ok
    assert parsed.account == "1234567"
    assert parsed.code == "2890"
    assert parsed.ord_seq == "448093"
    assert parsed.ord_no == "IJ904"
    assert parsed.status == "00"
    assert parsed.err == "order ok"


def test_parse_order_response_strips_t4_header() -> None:
    parsed = parse_order_response(_order_response())

    assert parsed.ok
    assert parsed.seqn == "00542731"
    assert parsed.branch == "S9A95"
    assert parsed.code == "2890"
    assert parsed.matched_price == "13.10"


def test_t4_broker_login_ca_and_stock_order() -> None:
    dll = FakeT4Dll()
    broker = T4Broker(_settings(), dll=dll)

    assert broker.login()
    assert broker.stock_account is not None
    assert broker.stock_account.branch == "9A95"
    assert broker.activate_ca()

    trade = broker.place_order(
        "2890",
        Action.Buy,
        1,
        price=13.0,
        price_type=StockPriceType.LMT,
        order_type=OrderType.ROD,
        custom_field="enter",
    )

    assert trade is not None
    assert trade.parsed.ord_no == "IJ904"
    assert ("stock_order2", ("B", "9A95", "1234567", "2890", "00", "13", "1", " ", "ROD")) in dll.calls


def test_t4_broker_market_sell_and_odd_lot_mapping() -> None:
    dll = FakeT4Dll()
    broker = T4Broker(_settings(), dll=dll)
    assert broker.login()

    broker.place_market_sell("2890", 1)
    broker.place_odd_lot_order("2890", Action.Buy, 10, 13.0)

    assert ("stock_order2", ("S", "9A95", "1234567", "2890", "00", "0", "1", "1", "IOC")) in dll.calls
    assert ("stock_order2", ("B", "9A95", "1234567", "2890", "C0", "13", "10", " ", "ROD")) in dll.calls


def test_poll_order_responses_dispatches_callback() -> None:
    dll = FakeT4Dll(response_queue=[_order_response()])
    broker = T4Broker(_settings(), dll=dll)
    seen = []
    broker.set_on_order(lambda stat, msg: seen.append((stat, msg)))

    parsed = broker.poll_order_responses()

    assert len(parsed) == 1
    assert seen[0][0] == OrderState.StockOrder
    assert seen[0][1]["operation"]["op_type"] == "T4Response"
    assert seen[0][1]["symbol"] == "2890"

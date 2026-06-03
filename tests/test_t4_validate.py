from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from bot.config import Settings
from bot.t4_validate import (
    default_exam1st_content,
    parse_t4_build,
    run_t4_validation,
)


def _settings(tmp_path: Path, **overrides) -> Settings:
    dll_path = tmp_path / "t4x64.dll"
    dll_path.write_bytes(b"fake")
    (tmp_path / "t4.ini").write_text("fake", encoding="ascii")
    (tmp_path / "SPSecuritiesATL_x64.dll").write_bytes(b"fake")
    defaults = dict(
        run_mode="trade",
        symbols=["2330"],
        t4_dll_path=str(dll_path),
        t4_login_id="A123456789",
        t4_login_password="pw",
        t4_person_id="A123456789",
        t4_ca_path=str(tmp_path / "Sinopac.pfx"),
        t4_ca_password="capw",
        _env_file=None,
    )
    Path(defaults["t4_ca_path"]).write_bytes(b"pfx")
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[call-arg]


def _field(value: str, width: int) -> str:
    return str(value)[:width].ljust(width)


def _stock_reply(**overrides: str) -> str:
    values = {
        "trade_type": "01",
        "account": "1234567",
        "code": "2890",
        "place_price": "17.00",
        "volume": "1",
        "ord_seq": "448093",
        "ord_date": "20260602",
        "effective_date": "20260602",
        "time": "150000",
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
    return "".join([
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
    ])


@dataclass
class FakeValidationDll:
    calls: List[Tuple[str, tuple]] = field(default_factory=list)

    def show_version(self) -> str:
        return "version: 10142"

    def show_ip(self) -> str:
        return "eleader.sinotrade.com.tw:443"

    def change_echo(self) -> str:
        return "echo ok"

    def get_response_evt(self) -> int:
        return 1

    def init_t4(self, login_id: str, login_pass: str, dll_path: str = "") -> str:
        self.calls.append(("init_t4", (login_id, login_pass, dll_path)))
        return "init ok"

    def show_list2(self) -> str:
        return "S9A95-1234567-Stock User\n"

    def add_acc_ca(self, branch: str, account: str, acc_id: str, acc_ca_path: str, acc_ca_pass: str) -> str:
        self.calls.append(("add_acc_ca", (branch, account, acc_id, acc_ca_path, acc_ca_pass)))
        return "ca ok"

    def verify_ca_pass(self, branch: str, account: str) -> str:
        self.calls.append(("verify_ca_pass", (branch, account)))
        return ""

    def do_register(self, yes_no: int) -> int:
        self.calls.append(("do_register", (yes_no,)))
        return 0

    def check_response_buffer(self) -> int:
        return 0

    def stock_order2(self, *args: str) -> str:
        self.calls.append(("stock_order2", args))
        return _stock_reply()

    def exam1st(self, user_id: str, branch: str, account: str, content: str) -> str:
        self.calls.append(("exam1st", (user_id, branch, account, content)))
        return "exam ok"

    def log_out(self) -> int:
        self.calls.append(("log_out", ()))
        return 0


def _test_time() -> dt.datetime:
    return dt.datetime(2026, 6, 2, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def test_parse_t4_build() -> None:
    assert parse_t4_build("version 10142 updated") == 10142
    assert parse_t4_build("1.0.14.2") == 10142


def test_t4_validation_skips_login_without_credentials(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        t4_login_id="",
        t4_login_password="",
        t4_person_id="",
        t4_ca_path="",
        t4_ca_password="",
    )
    report = run_t4_validation(
        settings,
        dll_factory=lambda path: FakeValidationDll(),
        current_time=_test_time(),
    )

    assert not report.can_use_t4_ordering
    assert any(c.name == "login" and c.status == "skip" for c in report.live)


def test_t4_validation_live_checks_do_not_promote_without_order_test(tmp_path: Path) -> None:
    report = run_t4_validation(
        _settings(tmp_path),
        dll_factory=lambda path: FakeValidationDll(),
        current_time=_test_time(),
    )

    assert report.can_use_t4_ordering
    assert not report.can_promote_to_t4
    assert any(c.name == "active_report" and c.status == "ok" for c in report.live)
    assert any(c.name == "stock_order2" and c.status == "skip" for c in report.order)


def test_t4_validation_promotes_after_exam1st(tmp_path: Path) -> None:
    dll = FakeValidationDll()
    report = run_t4_validation(
        _settings(tmp_path),
        attempt_exam1st=True,
        dll_factory=lambda path: dll,
        current_time=_test_time(),
    )

    assert report.can_promote_to_t4
    assert ("exam1st", ("A123456789", "9A95", "1234567", default_exam1st_content("2890", 17.0, 1))) in dll.calls

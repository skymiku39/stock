"""T4Broker -- adapter for Sinopac T4 DLL order routing.

This module keeps the first T4 integration step intentionally narrow:
login/account discovery, CA registration, domestic stock order submission,
and parsing of fixed-width stock/order report payloads.  It is designed to be
unit-testable with a fake DLL object, so tests never touch the real broker
endpoint or place real orders.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Protocol

from shioaji.constant import Action, OrderState, OrderType, StockOrderLot, StockPriceType

from bot.utils import get_logger


class T4Dll(Protocol):
    def init_t4(self, login_id: str, login_pass: str, dll_path: str = "") -> str: ...
    def log_out(self) -> int: ...
    def change_echo(self) -> str: ...
    def show_ip(self) -> str: ...
    def show_version(self) -> str: ...
    def show_list2(self) -> str: ...
    def add_acc_ca(
        self,
        branch: str,
        account: str,
        acc_id: str,
        acc_ca_path: str,
        acc_ca_pass: str,
    ) -> str: ...
    def verify_ca_pass(self, branch: str, account: str) -> str: ...
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
    ) -> str: ...
    def stock_order(
        self,
        buy_sell: str,
        branch: str,
        account: str,
        code: str,
        ord_type: str,
        price: str,
        amount: str,
        price_type: str,
    ) -> str: ...
    def stock_cancel(
        self,
        buy_sell: str,
        branch: str,
        account: str,
        code: str,
        ord_type: str,
        ord_seq: str,
        ord_no: str,
        pre_order: str,
    ) -> str: ...
    def stock_change(
        self,
        buy_sell: str,
        branch: str,
        account: str,
        code: str,
        ord_type: str,
        ord_seq: str,
        ord_no: str,
        pre_order: str,
        ord_knd: str,
        price: str,
        price_type: str,
    ) -> str: ...
    def future_order(
        self,
        buy_sell: str,
        branch: str,
        account: str,
        code: str,
        price: str,
        amount: str,
        price_type: str,
        ord_type: str,
        oct_type: str,
    ) -> str: ...
    def future_cancel(
        self,
        branch: str,
        account: str,
        code: str,
        ord_seq: str,
        ord_no: str,
        none: str,
        pre_order: str,
    ) -> str: ...
    def future_change(
        self,
        org_seqno: str,
        org_ordno: str,
        branch: str,
        account: str,
        code: str,
        new_price: str,
        pre_order: str,
    ) -> str: ...
    def option_order(
        self,
        buy_sell: str,
        branch: str,
        account: str,
        code: str,
        price: str,
        amount: str,
        price_type: str,
        ord_type: str,
        oct_type: str,
        is_comp: str,
        bs2: str,
        commodity2: str,
    ) -> str: ...
    def option_cancel(
        self,
        branch: str,
        account: str,
        code: str,
        ord_seq: str,
        ord_no: str,
        oct_type: str,
        pre_order: str,
    ) -> str: ...
    def option_change(
        self,
        org_seqno: str,
        org_ordno: str,
        branch: str,
        account: str,
        code: str,
        new_price: str,
        pre_order: str,
    ) -> str: ...
    def check_response_buffer(self) -> int: ...
    def get_response_log(self) -> str: ...
    def get_response(self) -> str: ...
    def timer_response_log(self) -> str: ...
    def get_response_evt(self) -> int: ...
    def do_register(self, yes_no: int) -> int: ...
    def stock_balance_qry(
        self,
        flag: str,
        leng: str,
        next_key: str,
        prev: str,
        gubn: str,
        group_name: str,
        branch: str,
        account: str,
        time_out: str,
    ) -> str: ...
    def stock_balance_sum(self, branch: str, account: str, ttype: str, action: str) -> str: ...
    def stock_balance_detail(self, branch: str, account: str, stock: str, ttype: str) -> str: ...
    def fo_order_qry2(
        self,
        branch: str,
        account: str,
        code: str,
        ord_match_flag: str,
        ord_type: str,
        oct_type: str,
        is_daily: str,
        start_date: str,
        end_date: str,
        preorder: str,
        source: str,
    ) -> str: ...
    def fo_unsettled_qry(
        self,
        flag: str,
        leng: str,
        next_key: str,
        prev: str,
        gubn: str,
        group_name: str,
        branch: str,
        account: str,
        type_1: str,
        type_2: str,
        time_out: str,
    ) -> str: ...
    def fo_get_hist_info(self, branch: str, account: str, start_date: str, end_date: str) -> str: ...
    def fo_get_day_info(self, branch: str, account: str) -> str: ...
    def exam1st(self, user_id: str, branch: str, account: str, content: str) -> str: ...


@dataclass(frozen=True)
class T4Account:
    raw: str
    market: str
    branch_with_market: str
    branch: str
    account: str
    name: str = ""

    @property
    def is_stock(self) -> bool:
        return self.market == "S"

    @property
    def is_future_option(self) -> bool:
        return self.market == "F"


@dataclass(frozen=True)
class T4ParsedReply:
    raw: str
    trade_type: str = ""
    account: str = ""
    code: str = ""
    place_price: str = ""
    volume: str = ""
    ord_seq: str = ""
    ord_date: str = ""
    effective_date: str = ""
    ord_no: str = ""
    web_id: str = ""
    org_ord_seq: str = ""
    ord_bs: str = ""
    time: str = ""
    status: str = ""
    err: str = ""
    ord_type: str = ""
    place_type: str = ""
    market_id: str = ""
    price_type: str = ""
    mprice_flag: str = ""
    ordknd: str = ""

    @property
    def ok(self) -> bool:
        if self.raw.startswith("TR Error") or self.raw.startswith("Error:"):
            return False
        return self.status in ("", "0", "00", "0000") or "\u6210\u529f" in self.err


@dataclass(frozen=True)
class T4ParsedResponse:
    raw: str
    seqn: str = ""
    branch: str = ""
    account: str = ""
    ord_no: str = ""
    ord_seq: str = ""
    code: str = ""
    ord_type: str = ""
    ord_class: str = ""
    place_price: str = ""
    matched_price: str = ""
    ordknd: str = ""
    volume: str = ""
    time: str = ""
    status: str = ""
    ecode: str = ""
    err: str = ""
    web_id: str = ""
    account_s: str = ""
    oct: str = ""
    ord_time: str = ""
    agent_id: str = ""
    price_type: str = ""
    tr_fld: str = ""
    matched_seqn: str = ""
    func_seqn: str = ""
    mprice_flag: str = ""

    @property
    def ok(self) -> bool:
        return self.ecode in ("", "00", "0000") or "\u6210\u529f" in self.err


@dataclass(frozen=True)
class T4Trade:
    raw_reply: str
    parsed: T4ParsedReply
    symbol: str
    action: str
    quantity: int
    price: float
    custom_field: str = ""

    @property
    def order(self) -> Any:
        return SimpleNamespace(
            ordno=self.parsed.ord_no,
            id=self.parsed.ord_seq,
        )

    @property
    def contract(self) -> Any:
        return SimpleNamespace(code=self.symbol)

    @property
    def status(self) -> Any:
        value = "Submitted" if self.parsed.ok else "Failed"
        return SimpleNamespace(status=SimpleNamespace(value=value))


class T4NativeDll:
    """Thin ctypes wrapper around t4.dll/t4x64.dll.

    Inputs are encoded as cp950 byte strings because the T4 document describes
    char*/MCB inputs rather than Unicode inputs.  The C# sample marks return
    values as AnsiBStr; in practice we read returned pointers as ANSI bytes.
    """

    def __init__(
        self,
        dll_path: str | Path,
        *,
        encoding: str = "cp950",
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("T4 DLL is only supported on Windows")
        self.path = Path(dll_path).resolve()
        self.encoding = encoding
        self._dll_dir_cookie = None
        if hasattr(os, "add_dll_directory"):
            self._dll_dir_cookie = os.add_dll_directory(str(self.path.parent))
        self._dll = ctypes.WinDLL(str(self.path))
        self._bind_functions()

    def _bind_bstr(self, name: str, arg_count: int) -> Any:
        func = getattr(self._dll, name)
        func.argtypes = [ctypes.c_char_p] * arg_count
        func.restype = ctypes.c_void_p
        return func

    def _try_bind_bstr(self, name: str, arg_count: int) -> Optional[Any]:
        try:
            return self._bind_bstr(name, arg_count)
        except AttributeError:
            return None

    def _bind_long(self, name: str, arg_count: int = 0) -> Any:
        func = getattr(self._dll, name)
        func.argtypes = [ctypes.c_long] * arg_count
        func.restype = ctypes.c_long
        return func

    def _try_bind_long(self, name: str, arg_count: int = 0) -> Optional[Any]:
        try:
            return self._bind_long(name, arg_count)
        except AttributeError:
            return None

    def _bind_ulong(self, name: str, arg_count: int = 0) -> Any:
        func = getattr(self._dll, name)
        func.argtypes = [ctypes.c_long] * arg_count
        func.restype = ctypes.c_uint32
        return func

    def _try_bind_ulong(self, name: str, arg_count: int = 0) -> Optional[Any]:
        try:
            return self._bind_ulong(name, arg_count)
        except AttributeError:
            return None

    def _bind_functions(self) -> None:
        self._fn_init_t4 = self._bind_bstr("init_t4", 3)
        self._fn_change_echo = self._try_bind_bstr("change_echo", 0)
        self._fn_show_ip = self._try_bind_bstr("show_ip", 0)
        self._fn_show_version = self._bind_bstr("show_version", 0)
        self._fn_show_list2 = self._bind_bstr("show_list2", 0)
        self._fn_add_acc_ca = self._bind_bstr("add_acc_ca", 5)
        self._fn_verify_ca_pass = self._bind_bstr("verify_ca_pass", 2)
        self._fn_stock_order = self._try_bind_bstr("stock_order", 8)
        self._fn_stock_order2 = self._bind_bstr("stock_order2", 9)
        self._fn_stock_cancel = self._bind_bstr("stock_cancel", 8)
        self._fn_stock_change = self._try_bind_bstr("stock_change", 11)
        self._fn_future_order = self._try_bind_bstr("future_order", 9)
        self._fn_future_cancel = self._try_bind_bstr("future_cancel", 7)
        self._fn_future_change = self._try_bind_bstr("future_change", 7)
        self._fn_option_order = self._try_bind_bstr("option_order", 12)
        self._fn_option_cancel = self._try_bind_bstr("option_cancel", 7)
        self._fn_option_change = self._try_bind_bstr("option_change", 7)
        self._fn_get_response_log = self._try_bind_bstr("get_response_log", 0)
        self._fn_get_response = self._try_bind_bstr("get_response", 0)
        self._fn_timer_response_log = self._bind_bstr("timer_response_log", 0)
        self._fn_get_response_evt = self._try_bind_ulong("get_response_evt")
        self._fn_log_out = self._bind_long("log_out")
        self._fn_check_response_buffer = self._bind_long("check_response_buffer")
        self._fn_do_register = self._bind_long("do_register", 1)
        self._fn_stock_balance_qry = self._try_bind_bstr("stock_balance_qry", 9)
        self._fn_stock_balance_sum = self._try_bind_bstr("stock_balance_sum", 4)
        self._fn_stock_balance_detail = self._try_bind_bstr("stock_balance_detail", 4)
        self._fn_fo_order_qry2 = self._try_bind_bstr("fo_order_qry2", 11)
        self._fn_fo_unsettled_qry = self._try_bind_bstr("fo_unsettled_qry", 11)
        self._fn_fo_get_hist_info = self._try_bind_bstr("fo_get_hist_info", 4)
        self._fn_fo_get_day_info = self._try_bind_bstr("fo_get_day_info", 2)
        self._fn_exam1st = self._try_bind_bstr("exam1st", 4)

    def _encode(self, value: str) -> bytes:
        return (value or "").encode(self.encoding, errors="replace")

    def _call_bstr(self, func: Any, *args: str) -> str:
        ptr = func(*[self._encode(arg) for arg in args])
        if not ptr:
            return ""
        raw = ctypes.cast(ptr, ctypes.c_char_p).value or b""
        return raw.decode(self.encoding, errors="replace")

    def _call_optional_bstr(self, func: Optional[Any], name: str, *args: str) -> str:
        if func is None:
            raise AttributeError(f"T4 DLL does not export {name}")
        return self._call_bstr(func, *args)

    def _call_optional_long(self, func: Optional[Any], name: str, *args: int) -> int:
        if func is None:
            raise AttributeError(f"T4 DLL does not export {name}")
        return int(func(*[int(arg) for arg in args]))

    def init_t4(self, login_id: str, login_pass: str, dll_path: str = "") -> str:
        return self._call_bstr(self._fn_init_t4, login_id, login_pass, dll_path)

    def log_out(self) -> int:
        return int(self._fn_log_out())

    def change_echo(self) -> str:
        return self._call_optional_bstr(self._fn_change_echo, "change_echo")

    def show_ip(self) -> str:
        return self._call_optional_bstr(self._fn_show_ip, "show_ip")

    def show_version(self) -> str:
        return self._call_bstr(self._fn_show_version)

    def show_list2(self) -> str:
        return self._call_bstr(self._fn_show_list2)

    def add_acc_ca(
        self,
        branch: str,
        account: str,
        acc_id: str,
        acc_ca_path: str,
        acc_ca_pass: str,
    ) -> str:
        return self._call_bstr(
            self._fn_add_acc_ca,
            branch,
            account,
            acc_id,
            acc_ca_path,
            acc_ca_pass,
        )

    def verify_ca_pass(self, branch: str, account: str) -> str:
        return self._call_bstr(self._fn_verify_ca_pass, branch, account)

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
        return self._call_bstr(
            self._fn_stock_order2,
            buy_sell,
            branch,
            account,
            code,
            ord_type,
            price,
            amount,
            price_type,
            ord_knd,
        )

    def stock_order(
        self,
        buy_sell: str,
        branch: str,
        account: str,
        code: str,
        ord_type: str,
        price: str,
        amount: str,
        price_type: str,
    ) -> str:
        return self._call_optional_bstr(
            self._fn_stock_order,
            "stock_order",
            buy_sell,
            branch,
            account,
            code,
            ord_type,
            price,
            amount,
            price_type,
        )

    def stock_cancel(
        self,
        buy_sell: str,
        branch: str,
        account: str,
        code: str,
        ord_type: str,
        ord_seq: str,
        ord_no: str,
        pre_order: str,
    ) -> str:
        return self._call_bstr(
            self._fn_stock_cancel,
            buy_sell,
            branch,
            account,
            code,
            ord_type,
            ord_seq,
            ord_no,
            pre_order,
        )

    def stock_change(
        self,
        buy_sell: str,
        branch: str,
        account: str,
        code: str,
        ord_type: str,
        ord_seq: str,
        ord_no: str,
        pre_order: str,
        ord_knd: str,
        price: str,
        price_type: str,
    ) -> str:
        return self._call_optional_bstr(
            self._fn_stock_change,
            "stock_change",
            buy_sell,
            branch,
            account,
            code,
            ord_type,
            ord_seq,
            ord_no,
            pre_order,
            ord_knd,
            price,
            price_type,
        )

    def future_order(
        self,
        buy_sell: str,
        branch: str,
        account: str,
        code: str,
        price: str,
        amount: str,
        price_type: str,
        ord_type: str,
        oct_type: str,
    ) -> str:
        return self._call_optional_bstr(
            self._fn_future_order,
            "future_order",
            buy_sell,
            branch,
            account,
            code,
            price,
            amount,
            price_type,
            ord_type,
            oct_type,
        )

    def future_cancel(
        self,
        branch: str,
        account: str,
        code: str,
        ord_seq: str,
        ord_no: str,
        none: str,
        pre_order: str,
    ) -> str:
        return self._call_optional_bstr(
            self._fn_future_cancel,
            "future_cancel",
            branch,
            account,
            code,
            ord_seq,
            ord_no,
            none,
            pre_order,
        )

    def future_change(
        self,
        org_seqno: str,
        org_ordno: str,
        branch: str,
        account: str,
        code: str,
        new_price: str,
        pre_order: str,
    ) -> str:
        return self._call_optional_bstr(
            self._fn_future_change,
            "future_change",
            org_seqno,
            org_ordno,
            branch,
            account,
            code,
            new_price,
            pre_order,
        )

    def option_order(
        self,
        buy_sell: str,
        branch: str,
        account: str,
        code: str,
        price: str,
        amount: str,
        price_type: str,
        ord_type: str,
        oct_type: str,
        is_comp: str,
        bs2: str,
        commodity2: str,
    ) -> str:
        return self._call_optional_bstr(
            self._fn_option_order,
            "option_order",
            buy_sell,
            branch,
            account,
            code,
            price,
            amount,
            price_type,
            ord_type,
            oct_type,
            is_comp,
            bs2,
            commodity2,
        )

    def option_cancel(
        self,
        branch: str,
        account: str,
        code: str,
        ord_seq: str,
        ord_no: str,
        oct_type: str,
        pre_order: str,
    ) -> str:
        return self._call_optional_bstr(
            self._fn_option_cancel,
            "option_cancel",
            branch,
            account,
            code,
            ord_seq,
            ord_no,
            oct_type,
            pre_order,
        )

    def option_change(
        self,
        org_seqno: str,
        org_ordno: str,
        branch: str,
        account: str,
        code: str,
        new_price: str,
        pre_order: str,
    ) -> str:
        return self._call_optional_bstr(
            self._fn_option_change,
            "option_change",
            org_seqno,
            org_ordno,
            branch,
            account,
            code,
            new_price,
            pre_order,
        )

    def check_response_buffer(self) -> int:
        return int(self._fn_check_response_buffer())

    def get_response_log(self) -> str:
        return self._call_optional_bstr(self._fn_get_response_log, "get_response_log")

    def get_response(self) -> str:
        return self._call_optional_bstr(self._fn_get_response, "get_response")

    def timer_response_log(self) -> str:
        return self._call_bstr(self._fn_timer_response_log)

    def get_response_evt(self) -> int:
        return self._call_optional_long(self._fn_get_response_evt, "get_response_evt")

    def do_register(self, yes_no: int) -> int:
        return int(self._fn_do_register(int(yes_no)))

    def stock_balance_qry(
        self,
        flag: str,
        leng: str,
        next_key: str,
        prev: str,
        gubn: str,
        group_name: str,
        branch: str,
        account: str,
        time_out: str,
    ) -> str:
        return self._call_optional_bstr(
            self._fn_stock_balance_qry,
            "stock_balance_qry",
            flag,
            leng,
            next_key,
            prev,
            gubn,
            group_name,
            branch,
            account,
            time_out,
        )

    def stock_balance_sum(self, branch: str, account: str, ttype: str, action: str) -> str:
        return self._call_optional_bstr(
            self._fn_stock_balance_sum,
            "stock_balance_sum",
            branch,
            account,
            ttype,
            action,
        )

    def stock_balance_detail(self, branch: str, account: str, stock: str, ttype: str) -> str:
        return self._call_optional_bstr(
            self._fn_stock_balance_detail,
            "stock_balance_detail",
            branch,
            account,
            stock,
            ttype,
        )

    def fo_order_qry2(
        self,
        branch: str,
        account: str,
        code: str,
        ord_match_flag: str,
        ord_type: str,
        oct_type: str,
        is_daily: str,
        start_date: str,
        end_date: str,
        preorder: str,
        source: str,
    ) -> str:
        return self._call_optional_bstr(
            self._fn_fo_order_qry2,
            "fo_order_qry2",
            branch,
            account,
            code,
            ord_match_flag,
            ord_type,
            oct_type,
            is_daily,
            start_date,
            end_date,
            preorder,
            source,
        )

    def fo_unsettled_qry(
        self,
        flag: str,
        leng: str,
        next_key: str,
        prev: str,
        gubn: str,
        group_name: str,
        branch: str,
        account: str,
        type_1: str,
        type_2: str,
        time_out: str,
    ) -> str:
        return self._call_optional_bstr(
            self._fn_fo_unsettled_qry,
            "fo_unsettled_qry",
            flag,
            leng,
            next_key,
            prev,
            gubn,
            group_name,
            branch,
            account,
            type_1,
            type_2,
            time_out,
        )

    def fo_get_hist_info(self, branch: str, account: str, start_date: str, end_date: str) -> str:
        return self._call_optional_bstr(
            self._fn_fo_get_hist_info,
            "fo_get_hist_info",
            branch,
            account,
            start_date,
            end_date,
        )

    def fo_get_day_info(self, branch: str, account: str) -> str:
        return self._call_optional_bstr(
            self._fn_fo_get_day_info,
            "fo_get_day_info",
            branch,
            account,
        )

    def exam1st(self, user_id: str, branch: str, account: str, content: str) -> str:
        return self._call_optional_bstr(
            self._fn_exam1st,
            "exam1st",
            user_id,
            branch,
            account,
            content,
        )

    def close(self) -> None:
        cookie = getattr(self, "_dll_dir_cookie", None)
        if cookie is not None:
            cookie.close()
            self._dll_dir_cookie = None


class T4Broker:
    """Order-routing adapter aligned to the small subset used by Strategy."""

    def __init__(
        self,
        settings: Any,
        logger: Optional[logging.Logger] = None,
        dll: Optional[T4Dll] = None,
    ) -> None:
        self.settings = settings
        self.logger = logger or get_logger("t4-broker")
        self.dll = dll or self._load_native_dll(settings)
        self.accounts: List[T4Account] = []
        self.stock_account: Optional[T4Account] = None
        self._trades: List[T4Trade] = []
        self._order_callback: Optional[Callable[[Any, dict], None]] = None
        self.last_order_error: Optional[Exception] = None

    def _load_native_dll(self, settings: Any) -> T4NativeDll:
        dll_path = getattr(settings, "t4_dll_path", "") or _default_t4_dll_path()
        return T4NativeDll(dll_path)

    def login(self) -> bool:
        login_id = getattr(self.settings, "t4_login_id", "") or getattr(self.settings, "person_id", "")
        login_pass = getattr(self.settings, "t4_login_password", "")
        dll_dir = getattr(self.settings, "t4_dll_dir", "")
        if not login_id or not login_pass:
            self.logger.error("T4 login requires T4_LOGIN_ID and T4_LOGIN_PASSWORD")
            return False
        ret = self.dll.init_t4(login_id, login_pass, dll_dir)
        self.logger.info("T4 init_t4: %s", ret)
        if _is_t4_error(ret):
            return False
        self.accounts = parse_accounts(self.dll.show_list2())
        self.stock_account = self._select_stock_account()
        self.logger.info("T4 accounts loaded: %d", len(self.accounts))
        return True

    def logout(self) -> None:
        try:
            self.dll.do_register(0)
        except Exception:
            self.logger.debug("T4 do_register(0) failed during logout", exc_info=True)
        try:
            self.dll.log_out()
        except Exception:
            self.logger.exception("T4 logout failed")

    def _select_stock_account(self) -> Optional[T4Account]:
        cfg_branch = str(getattr(self.settings, "t4_stock_branch", "") or "").strip()
        cfg_account = str(getattr(self.settings, "t4_stock_account", "") or "").strip()
        stock_accounts = [acc for acc in self.accounts if acc.is_stock]
        if cfg_account:
            for acc in stock_accounts:
                if acc.account == cfg_account and (not cfg_branch or acc.branch == _strip_market_prefix(cfg_branch)):
                    return acc
            return T4Account(
                raw=f"S{cfg_branch}-{cfg_account}",
                market="S",
                branch_with_market=_ensure_market_prefix(cfg_branch, "S"),
                branch=_strip_market_prefix(cfg_branch),
                account=cfg_account,
            )
        return stock_accounts[0] if stock_accounts else None

    def activate_ca(self, account: Optional[T4Account] = None) -> bool:
        target = account or self.stock_account
        if target is None:
            self.logger.error("No T4 stock account available for CA registration")
            return False
        ca_path = getattr(self.settings, "t4_ca_path", "") or getattr(self.settings, "ca_path", "")
        ca_pass = getattr(self.settings, "t4_ca_password", "") or getattr(self.settings, "ca_password", "")
        acc_id = getattr(self.settings, "t4_person_id", "") or getattr(self.settings, "person_id", "")
        ca_path = _normalize_t4_ca_path(ca_path)
        ret = self.dll.add_acc_ca(target.branch, target.account, acc_id, ca_path, ca_pass)
        self.logger.info("T4 add_acc_ca(%s-%s): %s", target.branch, target.account, ret)
        if _is_t4_error(ret):
            return False
        verify = self.dll.verify_ca_pass(target.branch, target.account)
        self.logger.info("T4 verify_ca_pass(%s-%s): %s", target.branch, target.account, verify)
        return not _is_t4_error(verify)

    def set_on_order(self, callback: Callable[[Any, dict], None]) -> None:
        self._order_callback = callback

    def place_order(
        self,
        symbol: str,
        action: Action,
        quantity: int,
        price: float = 0,
        price_type: StockPriceType = StockPriceType.LMT,
        order_type: OrderType = OrderType.ROD,
        custom_field: str = "",
        order_lot: StockOrderLot = StockOrderLot.Common,
    ) -> Optional[T4Trade]:
        if getattr(self.settings, "run_mode", "") != "trade":
            self.logger.error("Non-trade mode (%s) blocks T4 order", getattr(self.settings, "run_mode", ""))
            return None
        account = self.stock_account or self._select_stock_account()
        if account is None:
            self.logger.error("No T4 stock account available")
            return None

        try:
            buy_sell = map_action(action)
            ord_type = map_stock_ord_type(order_lot)
            t4_price_type = map_stock_price_type(price_type)
            ord_knd = map_order_type(order_type)
            reply = self.dll.stock_order2(
                buy_sell,
                account.branch,
                account.account,
                symbol,
                ord_type,
                _format_price(price),
                str(int(quantity)),
                t4_price_type,
                ord_knd,
            )
            parsed = parse_stock_reply(reply)
            trade = T4Trade(
                raw_reply=reply,
                parsed=parsed,
                symbol=symbol,
                action=buy_sell,
                quantity=int(quantity),
                price=float(price),
                custom_field=custom_field,
            )
            self.last_order_error = None
            self._trades.append(trade)
            if self._order_callback:
                self._order_callback(OrderState.StockOrder, t4_reply_to_msg(trade))
            return trade
        except Exception as exc:
            self.last_order_error = exc
            self.logger.exception("T4 stock order failed: %s", symbol)
            return None

    def place_market_sell(
        self,
        symbol: str,
        quantity: int,
        custom_field: str = "close",
    ) -> Optional[T4Trade]:
        return self.place_order(
            symbol=symbol,
            action=Action.Sell,
            quantity=quantity,
            price=0,
            price_type=StockPriceType.MKT,
            order_type=OrderType.IOC,
            custom_field=custom_field,
        )

    def place_odd_lot_order(
        self,
        symbol: str,
        action: Action,
        shares: int,
        price: float,
        custom_field: str = "odd",
    ) -> Optional[T4Trade]:
        return self.place_order(
            symbol=symbol,
            action=action,
            quantity=shares,
            price=price,
            price_type=StockPriceType.LMT,
            order_type=OrderType.ROD,
            custom_field=custom_field,
            order_lot=StockOrderLot.IntradayOdd,
        )

    def poll_order_responses(self, limit: int = 100) -> List[T4ParsedResponse]:
        out: List[T4ParsedResponse] = []
        for _ in range(max(0, int(limit))):
            if self.dll.check_response_buffer() <= 0:
                break
            raw = self.dll.timer_response_log()
            parsed = parse_order_response(raw)
            out.append(parsed)
            if self._order_callback:
                self._order_callback(OrderState.StockOrder, t4_response_to_msg(parsed))
        return out

    def register_responses(self, enabled: bool = True) -> int:
        return int(self.dll.do_register(1 if enabled else 0))

    def submit_exam1st(
        self,
        content: str,
        *,
        account: Optional[T4Account] = None,
        user_id: str = "",
    ) -> str:
        target = account or self.stock_account
        if target is None:
            raise RuntimeError("No T4 stock account available for exam1st")
        uid = user_id or getattr(self.settings, "t4_login_id", "") or getattr(self.settings, "person_id", "")
        return self.dll.exam1st(uid, target.branch, target.account, content)

    def update_status(self, trade: Optional[T4Trade] = None) -> None:
        self.poll_order_responses()

    def list_trades(self) -> List[T4Trade]:
        return list(self._trades)


def parse_accounts(text: str) -> List[T4Account]:
    accounts: List[T4Account] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("-")
        head = parts[0].strip()
        if len(head) < 2 or head[0] not in ("S", "F"):
            continue
        account = parts[1].strip() if len(parts) > 1 else ""
        name = parts[2].strip() if len(parts) > 2 else ""
        accounts.append(
            T4Account(
                raw=line,
                market=head[0],
                branch_with_market=head,
                branch=head[1:],
                account=account,
                name=name,
            )
        )
    return accounts


def parse_stock_reply(raw: str) -> T4ParsedReply:
    text = raw or ""
    if text.startswith("TR Error") or text.startswith("Error:") or len(text) < 149:
        return T4ParsedReply(raw=text, err=text.strip())
    pos = 0

    def take(n: int) -> str:
        nonlocal pos
        value = text[pos:pos + n].strip()
        pos += n
        return value

    return T4ParsedReply(
        raw=text,
        trade_type=take(2),
        account=take(15),
        code=take(6),
        place_price=take(9),
        volume=take(6),
        ord_seq=take(6),
        ord_date=take(8),
        effective_date=take(8),
        time=take(6),
        ord_no=take(5),
        web_id=take(3),
        org_ord_seq=take(6),
        ord_bs=take(1),
        ord_type=take(1),
        place_type=take(1),
        market_id=take(1),
        price_type=take(1),
        status=take(2),
        err=take(60),
        mprice_flag=take(1),
        ordknd=take(1),
    )


def parse_order_response(raw: str) -> T4ParsedResponse:
    text = raw or ""
    if len(text) >= 223:
        text = text[4:]
    if len(text) < 219:
        return T4ParsedResponse(raw=raw, err=text.strip())
    pos = 0

    def take(n: int) -> str:
        nonlocal pos
        value = text[pos:pos + n].strip()
        pos += n
        return value

    return T4ParsedResponse(
        raw=raw,
        seqn=take(8),
        branch=take(8),
        account=take(7),
        ord_no=take(5),
        ord_seq=take(6),
        code=take(10),
        ord_type=take(3),
        ord_class=take(2),
        place_price=take(10),
        matched_price=take(10),
        ordknd=take(3),
        volume=take(6),
        time=take(6),
        status=take(20),
        ecode=take(4),
        err=take(60),
        web_id=take(3),
        account_s=take(15),
        oct=take(1),
        ord_time=take(6),
        agent_id=take(6),
        price_type=take(1),
        tr_fld=take(4),
        matched_seqn=take(8),
        func_seqn=take(6),
        mprice_flag=take(1),
    )


def t4_reply_to_msg(trade: T4Trade) -> Dict[str, Any]:
    p = trade.parsed
    op_code = p.status or ("00" if p.ok else "99")
    return {
        "symbol": trade.symbol,
        "code": p.code or trade.symbol,
        "action": trade.action,
        "price": _to_float(p.place_price) or trade.price,
        "quantity": _to_int(p.volume) or trade.quantity,
        "ordno": p.ord_no,
        "ord_seq": p.ord_seq,
        "status": p.status,
        "message": p.err,
        "custom_field": trade.custom_field,
        "raw": trade.raw_reply,
        "operation": {
            "op_type": "T4Reply",
            "op_code": op_code,
            "op_msg": p.err,
        },
        "order": {
            "ordno": p.ord_no,
            "id": p.ord_seq,
            "action": trade.action,
            "price": _to_float(p.place_price) or trade.price,
            "quantity": _to_int(p.volume) or trade.quantity,
        },
        "contract": {
            "code": p.code or trade.symbol,
        },
    }


def t4_response_to_msg(resp: T4ParsedResponse) -> Dict[str, Any]:
    op_code = resp.ecode or ("00" if resp.ok else "99")
    return {
        "symbol": resp.code,
        "code": resp.code,
        "price": _to_float(resp.matched_price) or _to_float(resp.place_price),
        "quantity": _to_int(resp.volume),
        "ordno": resp.ord_no,
        "ord_seq": resp.ord_seq,
        "status": resp.status,
        "message": resp.err,
        "raw": resp.raw,
        "operation": {
            "op_type": "T4Response",
            "op_code": op_code,
            "op_msg": resp.err,
        },
        "order": {
            "ordno": resp.ord_no,
            "id": resp.ord_seq,
            "price": _to_float(resp.matched_price) or _to_float(resp.place_price),
            "quantity": _to_int(resp.volume),
        },
        "contract": {
            "code": resp.code,
        },
    }


def map_action(action: Action) -> str:
    value = getattr(action, "value", str(action))
    if value in ("Buy", "B", "\u8cb7"):
        return "B"
    if value in ("Sell", "S", "\u8ce3"):
        return "S"
    raise ValueError(f"Unsupported action for T4 stock order: {action!r}")


def map_stock_ord_type(order_lot: StockOrderLot) -> str:
    if order_lot == StockOrderLot.IntradayOdd:
        return "C0"
    if order_lot == StockOrderLot.Odd:
        return "20"
    return "00"


def map_stock_price_type(price_type: StockPriceType) -> str:
    if price_type == StockPriceType.MKT:
        return "1"
    if price_type == StockPriceType.LMT:
        return " "
    raise ValueError(f"Unsupported T4 stock price type: {price_type!r}")


def map_order_type(order_type: OrderType) -> str:
    value = getattr(order_type, "value", str(order_type))
    if value in ("ROD", "IOC", "FOK"):
        return value
    raise ValueError(f"Unsupported T4 order type: {order_type!r}")


def _format_price(price: float) -> str:
    if float(price) == 0:
        return "0"
    return f"{float(price):.4f}".rstrip("0").rstrip(".")


def _is_t4_error(text: str) -> bool:
    t = (text or "").strip()
    return t.startswith("Error:") or t.startswith("TR Error")


def _strip_market_prefix(branch: str) -> str:
    value = (branch or "").strip()
    if value[:1] in ("S", "F"):
        return value[1:]
    return value


def _ensure_market_prefix(branch: str, market: str) -> str:
    value = (branch or "").strip()
    return value if value.startswith(market) else market + value


def _default_t4_dll_path() -> str:
    return r"C:\vba dll\t4x64.dll" if sys.maxsize > 2**32 else r"C:\vba dll\t4.dll"


def _normalize_t4_ca_path(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    p = Path(value).expanduser()
    if p.suffix.lower() == ".pfx":
        return str(p.parent) + os.sep
    return value


def _to_float(value: str) -> float:
    try:
        return float((value or "").strip())
    except Exception:
        return 0.0


def _to_int(value: str) -> int:
    try:
        return int(float((value or "").strip()))
    except Exception:
        return 0


__all__ = [
    "T4Account",
    "T4Broker",
    "T4NativeDll",
    "T4ParsedReply",
    "T4ParsedResponse",
    "T4Trade",
    "map_action",
    "map_order_type",
    "map_stock_ord_type",
    "map_stock_price_type",
    "parse_accounts",
    "parse_order_response",
    "parse_stock_reply",
]

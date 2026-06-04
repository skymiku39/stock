"""T4 validation CLI and report helpers.

The validator is deliberately staged:

* Local checks are safe and always run.
* Login, account discovery, CA verification, and response registration run
  only when credentials are present and login is not disabled.
* exam1st and real order submission require explicit CLI flags because both
  are expected to leave records at Sinopac/T4.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from shioaji.constant import Action, OrderType, StockOrderLot, StockPriceType

from bot.config import Settings
from t4tools.broker import (
    T4Account,
    T4Broker,
    T4NativeDll,
    _default_t4_dll_path,
    _is_t4_error,
)
from bot.utils import now_tw


MIN_T4_BUILD = 10142


@dataclass
class T4Check:
    name: str
    status: str
    detail: str = ""
    suggestion: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class T4ValidationReport:
    generated_at: str
    can_use_t4_ordering: bool = False
    can_promote_to_t4: bool = False
    summary: str = ""
    local: List[T4Check] = field(default_factory=list)
    live: List[T4Check] = field(default_factory=list)
    order: List[T4Check] = field(default_factory=list)
    decision: List[T4Check] = field(default_factory=list)

    def all_checks(self) -> List[T4Check]:
        return self.local + self.live + self.order + self.decision

    def has_fail(self) -> bool:
        return any(c.status == "fail" for c in self.all_checks())

    def has_warn(self) -> bool:
        return any(c.status == "warn" for c in self.all_checks())


DllFactory = Callable[[str], Any]


def run_t4_validation(
    settings: Optional[Settings] = None,
    *,
    attempt_login: bool = True,
    attempt_order: bool = False,
    attempt_exam1st: bool = False,
    register_response: bool = True,
    read_queries: bool = False,
    order_symbol: str = "2890",
    order_price: float = 17.0,
    order_quantity: int = 1,
    exam_content: str = "",
    dll_factory: Optional[DllFactory] = None,
    current_time: Optional[dt.datetime] = None,
) -> T4ValidationReport:
    settings = settings or Settings()
    now = current_time or now_tw()
    report = T4ValidationReport(generated_at=now.isoformat(timespec="seconds"))

    dll_path = resolve_t4_dll_path(settings)
    dll_dir = dll_path.parent
    dll: Optional[Any] = None
    broker: Optional[T4Broker] = None

    report.local.extend(_check_local_files(dll_path, dll_dir))
    report.local.append(_check_test_window(now))

    if not any(c.name == "dll_path" and c.status == "fail" for c in report.local):
        try:
            factory = dll_factory or (lambda path: T4NativeDll(path))
            dll = factory(str(dll_path))
            report.local.append(T4Check("dll_load", "ok", str(dll_path)))
            _check_dll_exports(report, dll)
        except Exception as exc:  # noqa: BLE001
            report.local.append(T4Check(
                "dll_load",
                "fail",
                f"{type(exc).__name__}: {exc}",
                "Install the VC++ runtime and keep all files from the T4 bundle in one directory.",
            ))

    report.live.extend(_check_live_config(settings))

    login_ready = _has_login_config(settings)
    if not attempt_login:
        report.live.append(T4Check("login", "skip", "login disabled by --no-login"))
    elif dll is None:
        report.live.append(T4Check("login", "skip", "DLL did not load"))
    elif not login_ready:
        report.live.append(T4Check(
            "login",
            "skip",
            "missing T4_LOGIN_ID or T4_LOGIN_PASSWORD",
            "Fill .env T4_LOGIN_ID and T4_LOGIN_PASSWORD, then rerun stock-t4-validate.",
        ))
    else:
        broker = T4Broker(settings, dll=dll)
        _run_login_checks(report, broker)
        if broker.stock_account is not None and _has_ca_config(settings):
            _run_ca_checks(report, broker)
        elif broker.stock_account is not None:
            report.live.append(T4Check(
                "ca_verify",
                "skip",
                "missing T4 CA settings",
                "Fill T4_PERSON_ID, T4_CA_PATH, and T4_CA_PASSWORD to verify CA.",
            ))
        if broker.stock_account is not None and register_response:
            _run_response_checks(report, broker)
        if broker.stock_account is not None and read_queries:
            _run_read_query_checks(report, broker)

    if attempt_exam1st:
        _run_exam1st_check(report, broker, order_symbol, order_price, order_quantity, exam_content)
    else:
        report.order.append(T4Check(
            "exam1st",
            "skip",
            "not requested",
            "Use --exam1st only when you intentionally want to leave an official T4 first-test record.",
        ))

    if attempt_order:
        _run_order_check(report, broker, order_symbol, order_price, order_quantity)
    else:
        report.order.append(T4Check(
            "stock_order2",
            "skip",
            "not requested",
            "Use --order-test only in an approved T4 test window and with a test account/order plan.",
        ))

    _add_decision(report)

    if broker is not None:
        try:
            broker.logout()
        except Exception:  # noqa: BLE001
            pass
    elif dll is not None and hasattr(dll, "close"):
        try:
            dll.close()
        except Exception:  # noqa: BLE001
            pass

    return report


def resolve_t4_dll_path(settings: Settings) -> Path:
    if settings.t4_dll_path:
        return Path(settings.t4_dll_path).expanduser().resolve()
    repo_bundle = Path.cwd() / "docs" / "T4_10142" / "VBA"
    repo_dll = repo_bundle / ("t4x64.dll" if sys.maxsize > 2**32 else "t4.dll")
    if repo_dll.exists():
        return repo_dll.resolve()
    return Path(_default_t4_dll_path()).expanduser().resolve()


def is_t4_test_window(value: Optional[dt.datetime] = None) -> bool:
    now = value or now_tw()
    if now.weekday() >= 5:
        return False
    start = dt.time(14, 1)
    end = dt.time(17, 59)
    return start <= now.time() <= end


def report_to_dict(report: T4ValidationReport) -> Dict[str, Any]:
    return {
        "generated_at": report.generated_at,
        "can_use_t4_ordering": report.can_use_t4_ordering,
        "can_promote_to_t4": report.can_promote_to_t4,
        "summary": report.summary,
        "sections": {
            "local": [asdict(c) for c in report.local],
            "live": [asdict(c) for c in report.live],
            "order": [asdict(c) for c in report.order],
            "decision": [asdict(c) for c in report.decision],
        },
    }


def to_markdown(report: T4ValidationReport) -> str:
    rows = [
        "# T4 Validation Report",
        "",
        f"Generated at: {report.generated_at}",
        "",
        f"Summary: {report.summary}",
        "",
        f"Can use T4 ordering: {report.can_use_t4_ordering}",
        f"Can promote to T4 primary: {report.can_promote_to_t4}",
        "",
    ]
    for title, checks in (
        ("Local", report.local),
        ("Live", report.live),
        ("Order", report.order),
        ("Decision", report.decision),
    ):
        rows.append(f"## {title}")
        rows.append("")
        rows.append("| status | check | detail | suggestion |")
        rows.append("|:--:|---|---|---|")
        for check in checks:
            rows.append(
                "| {status} | {name} | {detail} | {suggestion} |".format(
                    status=check.status,
                    name=_md(check.name),
                    detail=_md(check.detail),
                    suggestion=_md(check.suggestion),
                )
            )
        rows.append("")
    return "\n".join(rows)


def _check_local_files(dll_path: Path, dll_dir: Path) -> List[T4Check]:
    out: List[T4Check] = []
    if dll_path.exists():
        out.append(T4Check("dll_path", "ok", str(dll_path)))
    else:
        out.append(T4Check(
            "dll_path",
            "fail",
            str(dll_path),
            "Set T4_DLL_PATH to docs/T4_10142/VBA/t4x64.dll or the installed T4 DLL path.",
        ))
        return out

    expected = ["t4.ini", "SPSecuritiesATL_x64.dll" if sys.maxsize > 2**32 else "SPSecuritiesATL.dll"]
    missing = [name for name in expected if not (dll_dir / name).exists()]
    if missing:
        out.append(T4Check(
            "bundle_files",
            "warn",
            "missing " + ", ".join(missing),
            "Keep t4.dll/t4x64.dll, t4.ini, and SPSecuritiesATL*.dll in the same bundle directory.",
        ))
    else:
        out.append(T4Check("bundle_files", "ok", str(dll_dir)))
    return out


def _check_test_window(now: dt.datetime) -> T4Check:
    detail = now.isoformat(timespec="seconds")
    if is_t4_test_window(now):
        return T4Check("official_test_window", "ok", detail)
    return T4Check(
        "official_test_window",
        "warn",
        detail,
        "Official T4 test window from the bundled test doc is business days 14:01-17:59 Taiwan time.",
    )


def _check_dll_exports(report: T4ValidationReport, dll: Any) -> None:
    try:
        version = dll.show_version()
        build = parse_t4_build(version)
        status = "ok" if build >= MIN_T4_BUILD else "fail"
        suggestion = "" if status == "ok" else "Upgrade to T4 API 1.0.14.2 or newer."
        report.local.append(T4Check(
            "show_version",
            status,
            version,
            suggestion,
            {"build": build},
        ))
    except Exception as exc:  # noqa: BLE001
        report.local.append(T4Check("show_version", "fail", f"{type(exc).__name__}: {exc}"))

    report.local.append(T4Check(
        "change_echo",
        "skip",
        "not invoked because it toggles order echo behavior",
    ))
    report.local.append(T4Check(
        "get_response_evt",
        "skip",
        "not invoked because it creates a native event handle",
    ))

    for name in ("show_ip",):
        if not hasattr(dll, name):
            report.local.append(T4Check(name, "warn", "wrapper missing"))
            continue
        try:
            value = getattr(dll, name)()
            report.local.append(T4Check(name, "ok", _short(value)))
        except AttributeError as exc:
            report.local.append(T4Check(name, "warn", str(exc)))
        except Exception as exc:  # noqa: BLE001
            report.local.append(T4Check(name, "warn", f"{type(exc).__name__}: {exc}"))


def parse_t4_build(version: str) -> int:
    text = version or ""
    matches = re.findall(r"\d+", text)
    for item in matches:
        if len(item) >= 5:
            return int(item[:5])
    joined = "".join(matches[:4])
    return int(joined) if joined.isdigit() else 0


def _check_live_config(settings: Settings) -> List[T4Check]:
    out = []
    if _has_login_config(settings):
        out.append(T4Check("login_config", "ok", _mask(settings.t4_login_id or settings.person_id)))
    else:
        out.append(T4Check(
            "login_config",
            "warn",
            "missing login id/password",
            "Set T4_LOGIN_ID and T4_LOGIN_PASSWORD.",
        ))

    ca_path = settings.t4_ca_path or settings.ca_path
    if _has_ca_config(settings):
        exists = Path(ca_path).expanduser().exists()
        out.append(T4Check(
            "ca_config",
            "ok" if exists else "fail",
            f"path_exists={exists}",
            "" if exists else "Set T4_CA_PATH to a valid Sinopac PFX file or certificate directory.",
        ))
    else:
        out.append(T4Check(
            "ca_config",
            "warn",
            "missing CA settings",
            "Set T4_PERSON_ID, T4_CA_PATH, and T4_CA_PASSWORD.",
        ))
    return out


def _run_login_checks(report: T4ValidationReport, broker: T4Broker) -> None:
    try:
        ok = broker.login()
    except Exception as exc:  # noqa: BLE001
        report.live.append(T4Check("login", "fail", f"{type(exc).__name__}: {exc}"))
        return
    report.live.append(T4Check("login", "ok" if ok else "fail", "init_t4"))
    if not ok:
        return

    accounts = broker.accounts
    stock = [acc for acc in accounts if acc.is_stock]
    fo = [acc for acc in accounts if acc.is_future_option]
    status = "ok" if accounts else "fail"
    report.live.append(T4Check(
        "show_list2",
        status,
        f"accounts={len(accounts)} stock={len(stock)} future_option={len(fo)}",
        "If this is empty, complete official API signing/testing or verify account permission.",
        {"accounts": [_account_to_safe_dict(acc) for acc in accounts]},
    ))
    if broker.stock_account is None:
        report.live.append(T4Check(
            "stock_account",
            "fail",
            "no stock account selected",
            "Set T4_STOCK_BRANCH/T4_STOCK_ACCOUNT or verify show_list2 contains an S account.",
        ))
    else:
        report.live.append(T4Check(
            "stock_account",
            "ok",
            f"branch={broker.stock_account.branch} account={_mask(broker.stock_account.account)}",
        ))


def _run_ca_checks(report: T4ValidationReport, broker: T4Broker) -> None:
    try:
        ok = broker.activate_ca()
    except Exception as exc:  # noqa: BLE001
        report.live.append(T4Check("ca_verify", "fail", f"{type(exc).__name__}: {exc}"))
        return
    report.live.append(T4Check(
        "ca_verify",
        "ok" if ok else "fail",
        "add_acc_ca + verify_ca_pass",
        "Check T4_PERSON_ID, T4_CA_PATH, T4_CA_PASSWORD, and certificate validity." if not ok else "",
    ))


def _run_response_checks(report: T4ValidationReport, broker: T4Broker) -> None:
    try:
        on = broker.register_responses(True)
        count = broker.dll.check_response_buffer()
        off = broker.register_responses(False)
        report.live.append(T4Check(
            "active_report",
            "ok",
            f"register_on={on} buffer={count} register_off={off}",
        ))
    except Exception as exc:  # noqa: BLE001
        report.live.append(T4Check("active_report", "fail", f"{type(exc).__name__}: {exc}"))


def _run_read_query_checks(report: T4ValidationReport, broker: T4Broker) -> None:
    account = broker.stock_account
    if account is None:
        return
    calls = [
        ("stock_balance_sum", lambda: broker.dll.stock_balance_sum(account.branch, account.account, "0", "0")),
        ("stock_balance_detail", lambda: broker.dll.stock_balance_detail(account.branch, account.account, "", "0")),
        (
            "stock_balance_qry",
            lambda: broker.dll.stock_balance_qry("0", "100", "", "", "0", "", account.branch, account.account, "10"),
        ),
    ]
    for name, fn in calls:
        try:
            value = fn()
            report.live.append(T4Check(name, "ok" if not _is_t4_error(value) else "fail", _short(value)))
        except AttributeError as exc:
            report.live.append(T4Check(name, "warn", str(exc)))
        except Exception as exc:  # noqa: BLE001
            report.live.append(T4Check(name, "fail", f"{type(exc).__name__}: {exc}"))


def _run_exam1st_check(
    report: T4ValidationReport,
    broker: Optional[T4Broker],
    symbol: str,
    price: float,
    quantity: int,
    content: str,
) -> None:
    if broker is None or broker.stock_account is None:
        report.order.append(T4Check("exam1st", "skip", "login/stock account unavailable"))
        return
    payload = content or default_exam1st_content(symbol, price, quantity)
    try:
        value = broker.submit_exam1st(payload)
        report.order.append(T4Check("exam1st", "ok" if not _is_t4_error(value) else "fail", _short(value)))
    except Exception as exc:  # noqa: BLE001
        report.order.append(T4Check("exam1st", "fail", f"{type(exc).__name__}: {exc}"))


def _run_order_check(
    report: T4ValidationReport,
    broker: Optional[T4Broker],
    symbol: str,
    price: float,
    quantity: int,
) -> None:
    if broker is None or broker.stock_account is None:
        report.order.append(T4Check("stock_order2", "skip", "login/stock account unavailable"))
        return
    try:
        trade = broker.place_order(
            symbol=symbol,
            action=Action.Buy,
            quantity=quantity,
            price=price,
            price_type=StockPriceType.LMT,
            order_type=OrderType.ROD,
            order_lot=StockOrderLot.Common,
            custom_field="t4test",
        )
        if trade is None:
            report.order.append(T4Check("stock_order2", "fail", "place_order returned None"))
        else:
            report.order.append(T4Check(
                "stock_order2",
                "ok" if trade.parsed.ok else "fail",
                _short(trade.raw_reply),
                "" if trade.parsed.ok else trade.parsed.err,
                {"ord_no": trade.parsed.ord_no, "ord_seq": trade.parsed.ord_seq},
            ))
    except Exception as exc:  # noqa: BLE001
        report.order.append(T4Check("stock_order2", "fail", f"{type(exc).__name__}: {exc}"))


def _add_decision(report: T4ValidationReport) -> None:
    dll_ok = any(c.name == "show_version" and c.status == "ok" for c in report.local)
    login_ok = any(c.name == "login" and c.status == "ok" for c in report.live)
    stock_ok = any(c.name == "stock_account" and c.status == "ok" for c in report.live)
    ca_ok = any(c.name == "ca_verify" and c.status == "ok" for c in report.live)
    order_ok = any(c.name in ("stock_order2", "exam1st") and c.status == "ok" for c in report.order)

    report.can_use_t4_ordering = bool(dll_ok and login_ok and stock_ok and ca_ok)
    report.can_promote_to_t4 = bool(report.can_use_t4_ordering and order_ok)

    if not report.can_use_t4_ordering:
        report.decision.append(T4Check(
            "promotion",
            "fail",
            "T4 ordering is not fully validated yet.",
            "Complete login, show_list2 stock account, and CA verification first.",
        ))
    elif not order_ok:
        report.decision.append(T4Check(
            "promotion",
            "warn",
            "T4 login/account/CA are valid, but no official order-path test has passed.",
            "Run --exam1st or --order-test in the official test window before switching order routing.",
        ))
    else:
        report.decision.append(T4Check(
            "promotion",
            "ok",
            "T4 order path has passed validation.",
        ))

    report.decision.append(T4Check(
        "market_data_gap",
        "warn",
        "The T4 DLL bundle validates order/account functions, not Shioaji-like tick subscriptions.",
        "Current strategy still needs Shioaji or another live market data source for trade/watch mode.",
    ))
    if report.can_promote_to_t4:
        report.summary = "T4 order routing is validated; full Shioaji replacement still needs market-data parity."
    elif report.can_use_t4_ordering:
        report.summary = "T4 login/account/CA are validated; order-path test is still missing."
    else:
        report.summary = "T4 cannot be promoted yet; required live checks are missing or failed."


def _has_login_config(settings: Settings) -> bool:
    return bool((settings.t4_login_id or settings.person_id) and settings.t4_login_password)


def _has_ca_config(settings: Settings) -> bool:
    ca_path = settings.t4_ca_path or settings.ca_path
    ca_pass = settings.t4_ca_password or settings.ca_password
    return bool((settings.t4_person_id or settings.person_id) and ca_path and ca_pass)


def _account_to_safe_dict(account: T4Account) -> Dict[str, str]:
    return {
        "market": account.market,
        "branch": account.branch,
        "account": _mask(account.account),
        "name_present": str(bool(account.name)),
    }


def default_exam1st_content(symbol: str, price: float, quantity: int) -> str:
    return ",".join([
        "B",
        str(symbol),
        _format_decimal(price),
        str(quantity),
        "LMT",
        "ROD",
        "B",
        "TXFE2",
        "15775",
        "1",
        "LMT",
        "ROD",
    ])


def _format_decimal(value: float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _short(value: Any, limit: int = 180) -> str:
    text = str(value or "").replace("\r", "\\r").replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "..."


def _mask(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


def _md(value: str) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-t4-validate", description="Validate Sinopac T4 DLL functions")
    parser.add_argument("--no-login", action="store_true", help="Only run local DLL checks")
    parser.add_argument("--read-queries", action="store_true", help="Run read-only account query functions after CA")
    parser.add_argument("--exam1st", action="store_true", help="Submit official T4 first-test exam1st payload")
    parser.add_argument("--order-test", action="store_true", help="Submit a real T4 stock_order2 test order")
    parser.add_argument("--symbol", default="2890", help="Stock symbol for exam/order tests")
    parser.add_argument("--price", type=float, default=17.0, help="Limit price for exam/order tests")
    parser.add_argument("--quantity", type=int, default=1, help="Quantity for exam/order tests")
    parser.add_argument("--exam-content", default="", help="Override exam1st content payload")
    parser.add_argument("--json", dest="json_path", default="", help="Write JSON report to path")
    parser.add_argument("--md", dest="md_path", default="", help="Write Markdown report to path")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    report = run_t4_validation(
        attempt_login=not args.no_login,
        attempt_order=args.order_test,
        attempt_exam1st=args.exam1st,
        read_queries=args.read_queries,
        order_symbol=args.symbol,
        order_price=args.price,
        order_quantity=args.quantity,
        exam_content=args.exam_content,
    )

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report_to_dict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.md_path:
        Path(args.md_path).write_text(to_markdown(report), encoding="utf-8")

    print(to_markdown(report))
    if args.json_path:
        print(f"JSON written: {args.json_path}")
    if args.md_path:
        print(f"Markdown written: {args.md_path}")
    return 0 if report.can_promote_to_t4 else 2


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "T4Check",
    "T4ValidationReport",
    "default_exam1st_content",
    "is_t4_test_window",
    "parse_t4_build",
    "report_to_dict",
    "run_t4_validation",
    "to_markdown",
]

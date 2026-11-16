"""資料新鮮度檢查：每一類資料最新到哪一天、是否落後。唯讀、不連網、不呼叫模型。

為什麼需要：排程器活著、工作回報成功，不代表資料有更新（2026-11 曾發生日 K 只更新兩檔、
ETF 持股與籌碼停更數日而沒有任何警示）。這裡直接看資料本身的日期。

用法::

    python -m bot.data_freshness --root C:\\skymiku\\stock [--json]

結束碼：0 全部正常、1 有警告、2 有落後。假日無法從資料判斷，所以落後一個工作日只算警告。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from bot.runtime_state import read_json
from bot.utils import now_tw

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
RETIRED_JOB_DAYS = 14
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def expected_market_date(now: dt.datetime, ready_hour: int = 16) -> dt.date:
    """最近一個「收盤資料應該已經公布」的工作日（不含假日判斷）。"""
    day = now.date()
    if day.weekday() >= 5 or now.hour < ready_hour:
        day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def weekday_lag(latest: str, expected: dt.date) -> Optional[int]:
    """latest 之後到 expected（含）之間有幾個工作日；latest 無法解析回 None。"""
    try:
        day = dt.date.fromisoformat(str(latest)[:10])
    except ValueError:
        return None
    lag = 0
    while day < expected:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            lag += 1
    return lag


def _grade(lag: Optional[int], warn_at: int, fail_at: int) -> str:
    if lag is None:
        return FAIL
    return FAIL if lag >= fail_at else WARN if lag >= warn_at else OK


def _check(name: str, status: str, detail: str, **extra: Any) -> Dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, **({"extra": extra} if extra else {})}


def _dated(latest: str, expected: dt.date, name: str, warn_at: int, fail_at: int, what: str = "") -> Dict[str, Any]:
    if not latest:
        return _check(name, FAIL, "沒有任何資料")
    lag = weekday_lag(latest, expected)
    tail = "" if not lag else f"，落後 {lag} 個工作日（預期 {expected.isoformat()}；遇假日屬正常）"
    return _check(name, _grade(lag, warn_at, fail_at), f"最新 {latest}{what}{tail}", latest=latest, lag=lag)


def _newest_date_in_names(directory: Path) -> str:
    try:
        names = [m.group(1) for p in directory.iterdir() if (m := _DATE.search(p.name))]
    except OSError:
        return ""
    return max(names, default="")


def _hours_since(value: str, now: dt.datetime) -> Optional[float]:
    try:
        then = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=now.tzinfo)
    return (now - then).total_seconds() / 3600


def _scheduler(root: Path, now: dt.datetime) -> List[Dict[str, Any]]:
    status = read_json(root / "data/scheduler_status.json")
    if not status:
        return [_check("排程器", FAIL, "找不到 data/scheduler_status.json")]
    age = _hours_since(status.get("heartbeat_at", ""), now)
    out = [_check("排程器心跳", OK if age is not None and age * 60 <= 10 else FAIL,
                  f"最後心跳 {status.get('heartbeat_at', '未知')}")]
    problems, retired = [], []
    for name, job in sorted((status.get("jobs") or {}).items()):
        state = job.get("state", "")
        if state in {"succeeded", "running", "busy", "dry_run"}:
            continue
        # 狀態檔會留著已從設定停用的工作；兩週沒有任何動靜的視為已停用，不當成問題。
        idle = _hours_since(job.get("finished_at") or job.get("started_at") or "", now)
        if idle is not None and idle > RETIRED_JOB_DAYS * 24:
            retired.append(name)
            continue
        problems.append(f"{name}：{state}（最後成功 {job.get('last_success_at') or '無紀錄'}；"
                        f"下次重試 {job.get('next_retry_at') or '未排定'}）")
    out.append(_check("排程工作", WARN if problems else OK,
                      ("；".join(problems) if problems else "執行中的工作上次都成功")
                      + (f"（{RETIRED_JOB_DAYS} 天以上沒有執行、視為已停用：{'、'.join(retired)}）" if retired else "")))
    return out


def _news(root: Path, now: dt.datetime) -> List[Dict[str, Any]]:
    out = []
    latest, count = "", 0
    for offset in (0, 1):
        day = (now.date() - dt.timedelta(days=offset)).isoformat()
        payload = read_json(root / "data/news" / f"news_{day}.json")
        items = payload.get("items") or []
        if items:
            latest = max((str(i.get("published_at") or "") for i in items), default="")
            count = len(items)
            break
    age = _hours_since(latest, now)
    if age is None:
        out.append(_check("新聞（鉅亨）", FAIL, "今天與昨天都沒有新聞日快取"))
    else:
        out.append(_check("新聞（鉅亨）", FAIL if age > 72 else WARN if age > 30 else OK,
                          f"最新一則發布於 {latest}（{age:.0f} 小時前），日快取 {count} 則"))
    rss = read_json(root / "data/news" / f"rss_{now.date().isoformat()}.json")
    if not rss:
        out.append(_check("RSS 存檔", WARN if now.hour < 1 else FAIL, "今天還沒有 RSS 存檔"))
    else:
        total, good = int(rss.get("feeds_total") or 0), int(rss.get("feeds_ok") or 0)
        age = _hours_since(rss.get("collected_at", ""), now)
        bad = age is None or age > 3 or (total and good / total < 0.7)
        out.append(_check("RSS 存檔", WARN if bad else OK,
                          f"來源 {good}/{total} 正常，累積 {rss.get('total_items', 0)} 則，"
                          f"上次收集 {rss.get('collected_at', '未知')}",
                          failed=rss.get("feeds_failed", [])))
    return out


def _prices(root: Path, now: dt.datetime, core: List[str], extended: List[str]) -> List[Dict[str, Any]]:
    db_path = root / "data/stock.db"
    if not db_path.exists():
        return [_check("日 K", FAIL, "找不到 data/stock.db")]
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        latest = dict(con.execute("SELECT symbol, MAX(date) FROM price_history GROUP BY symbol"))
        valuation = con.execute("SELECT MAX(as_of) FROM valuation_daily").fetchone()[0] or ""
        revenue = con.execute("SELECT MAX(year_month) FROM monthly_revenue").fetchone()[0] or ""
    finally:
        con.close()
    expected = expected_market_date(now)
    out = []
    core_latest = min((latest.get(s, "") for s in core), default="")
    out.append(_dated(core_latest, expected, "日 K（主要追蹤）", 1, 3, f"（{'、'.join(core) or '未設定'}）"))
    reference = max((latest.get(s, "") for s in core), default="") or max(latest.values(), default="")
    if extended:
        behind = sorted(s for s in extended if latest.get(s, "") < reference)
        share = 1 - len(behind) / len(extended)
        sample = "、".join(f"{s}（{latest.get(s) or '無'}）" for s in behind[:8])
        out.append(_check("日 K（ETF、ETF 持股、回測名單）",
                          FAIL if share < 0.8 else WARN if share < 0.95 else OK,
                          f"{len(extended) - len(behind)}/{len(extended)} 檔已到 {reference or '未知'}"
                          + (f"；落後：{sample}{' 等' if len(behind) > 8 else ''}" if behind else ""),
                          behind=behind))
    out.append(_dated(str(valuation)[:10], expected, "本益比／殖利率", 3, 6))
    month = now.date().replace(day=1)
    for _ in range(1 if now.day >= 12 else 2):       # 月營收在次月 10 日前公布
        month = (month - dt.timedelta(days=1)).replace(day=1)
    want = month.strftime("%Y-%m")
    got = str(revenue).replace("/", "-")[:7]
    out.append(_check("月營收", OK if got >= want else WARN if got else FAIL,
                      f"最新 {got or '無'}（預期至少 {want}）"))
    return out


def _etf_holdings(root: Path, now: dt.datetime) -> Dict[str, Any]:
    from bot.active_etf import list_holdings_dates, load_active_etfs
    expected = expected_market_date(now, ready_hour=20)
    stale, missing, total = [], [], 0
    for etf in load_active_etfs(root):
        total += 1
        dates = list_holdings_dates(etf.symbol, root)
        if not dates:
            missing.append(etf.symbol)
        elif (weekday_lag(dates[0].isoformat(), expected) or 0) >= 3:
            stale.append(f"{etf.symbol}（{dates[0].isoformat()}）")
    bad = len(stale) + len(missing)
    detail = f"{total - bad}/{total} 檔在三個工作日內有持股資料"
    if stale:
        detail += "；過期：" + "、".join(stale[:10]) + (" 等" if len(stale) > 10 else "")
    if missing:
        detail += "；沒有資料：" + "、".join(missing[:10])
    return _check("ETF 持股", OK if not bad else FAIL if total and bad / total > 0.5 else WARN, detail)


def _model(root: Path, now: dt.datetime) -> Dict[str, Any]:
    state = read_json(root / "data/llm_availability.json")
    if not state:
        return _check("模型服務", INFO, "沒有可用性紀錄")
    if state.get("status") != "blocked":
        return _check("模型服務", OK, f"狀態 {state.get('status', '未知')}")
    retry = state.get("next_retry_at")
    when = (dt.datetime.fromtimestamp(float(retry), now.tzinfo).isoformat(timespec="minutes")
            if isinstance(retry, (int, float)) and retry else "未知")
    return _check("模型服務", WARN, f"被擋：{state.get('reason', '未知原因')}；下次重試 {when}。"
                                     "需要模型的研究與草稿在此之前不會產生")


def check_data_freshness(root: Path, *, settings: Any = None, now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    now = now or now_tw()
    core: List[str] = sorted(getattr(settings, "symbols", None) or [])
    try:
        from bot.daily_collection import extended_price_symbols
        extended = [s for s in extended_price_symbols(root) if s not in core]
    except Exception:
        extended = []
    expected = expected_market_date(now)
    steps: List[Callable[[], Any]] = [
        lambda: _scheduler(root, now),
        lambda: _news(root, now),
        lambda: _prices(root, now, core, extended),
        lambda: _etf_holdings(root, now),
        lambda: _dated(_newest_date_in_names(root / "data/chips"), expected, "籌碼（三大法人、融資券）", 2, 4),
        lambda: _dated(_newest_date_in_names(root / "data/macro"), expected, "跨市場總經", 2, 4),
        lambda: _model(root, now),
    ]
    checks: List[Dict[str, Any]] = []
    for step in steps:
        try:
            result = step()
            checks.extend(result if isinstance(result, list) else [result])
        except Exception as exc:  # 單一檢查壞掉不該讓整份報告消失
            checks.append(_check("檢查失敗", FAIL, f"{type(exc).__name__}: {exc}"))
    worst = FAIL if any(c["status"] == FAIL for c in checks) else WARN if any(c["status"] == WARN for c in checks) else OK
    return {"checked_at": now.isoformat(timespec="seconds"), "expected_market_date": expected.isoformat(),
            "status": worst, "checks": checks}


def format_report(report: Dict[str, Any]) -> List[str]:
    label = {OK: "正常", WARN: "警告", FAIL: "落後", INFO: "資訊"}
    lines = [f"資料新鮮度（{report['checked_at']}；預期最新交易日 {report['expected_market_date']}）：{label[report['status']]}"]
    lines += [f"- [{label[c['status']]}] {c['name']}：{c['detail']}" for c in report["checks"]]
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    from bot.cli_encoding import configure_console
    from bot.config import Settings
    configure_console()
    parser = argparse.ArgumentParser(description="檢查各類資料最新到哪一天（唯讀）")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    report = check_data_freshness(root, settings=Settings(_env_file=str(root / ".env")))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else "\n".join(format_report(report)))
    return {OK: 0, WARN: 1, FAIL: 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())

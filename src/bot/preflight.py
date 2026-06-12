"""preflight -- 「我現在到底能不能下單？」一鍵體檢工具。

設計
====
跑一次 `run_preflight(settings)` 後，回傳一份 `PreflightReport`，
裡面是七個區塊、每個區塊一連串 `CheckResult`：

1. **環境變數 (essentials)** — API_KEY / SECRET_KEY / RUN_MODE / SIMULATION
2. **電子憑證 (CA)** — 檔案存在、密碼/身分證設定、過期日
3. **Shioaji 連線** — 真實登入測試 (拿到 stock_account 才算通過)
4. **帳戶權限** — stock_account.signed、帳號類型 (現股/信用)、可用資金
5. **風控設定** — 停損/停利合理性、max_fund、max_lot_per_symbol
6. **時間窗口** — 現在是否在盤中、距離開盤/收盤多久、是否為交易日
7. **持倉安全** — 監控標的手動持股重疊、本工具 AI 紀錄對帳

CheckResult.status:
  ok    — 通過
  warn  — 通過但需注意 (例：simulation=True 表示不是真實下單)
  fail  — 阻擋；不解決就無法下單
  info  — 純資訊

對應的核心動作：
* CLI: `uv run stock-preflight`
* Dashboard: 啟動 / 監控 頁的「交易可行性檢查」按鈕
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.archive_status import day_trading_trade_blocked
from bot.config import Settings
from bot.utils import get_logger, now_tw


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    status: str           # ok | warn | fail | info
    detail: str = ""
    suggestion: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PreflightReport:
    fetched_at: str
    can_trade_now: bool = False    # 「現在馬上能不能下真實單」最終結論
    can_simulate: bool = False     # 能否跑模擬單
    section_env: List[CheckResult] = field(default_factory=list)
    section_ca: List[CheckResult] = field(default_factory=list)
    section_login: List[CheckResult] = field(default_factory=list)
    section_account: List[CheckResult] = field(default_factory=list)
    section_risk: List[CheckResult] = field(default_factory=list)
    section_time: List[CheckResult] = field(default_factory=list)
    section_safety: List[CheckResult] = field(default_factory=list)
    summary: str = ""

    def all_checks(self) -> List[CheckResult]:
        return (
            self.section_env + self.section_ca + self.section_login
            + self.section_account + self.section_risk + self.section_time
            + self.section_safety
        )

    def has_fail(self) -> bool:
        return any(c.status == "fail" for c in self.all_checks())

    def has_warn(self) -> bool:
        return any(c.status == "warn" for c in self.all_checks())


# ----------------------------------------------------------------------
# 個別檢查
# ----------------------------------------------------------------------


def _format_watch_pool_detail(
    symbols: List[str],
    *,
    auto_merge: bool,
    source_counts: Optional[Dict[str, int]] = None,
) -> str:
    preview = ",".join(symbols[:8])
    if len(symbols) > 8:
        preview += f" … 共 {len(symbols)} 檔"
    else:
        preview = f"{len(symbols)} 檔: {preview}"
    if auto_merge and source_counts:
        preview += (
            f" | 四源 manual={source_counts.get('manual', 0)}"
            f" 今日={source_counts.get('intraday_today', 0)}"
            f" 昨日={source_counts.get('intraday_prev', 0)}"
            f" 明日={source_counts.get('nextday_prev', 0)}"
            f" 即時={source_counts.get('live_open', 0)}"
        )
    return preview


def _check_env(
    settings: Settings,
    *,
    watch_pool_source_counts: Optional[Dict[str, int]] = None,
) -> List[CheckResult]:
    out: List[CheckResult] = []

    # API Key / Secret Key
    if settings.api_key and settings.secret_key:
        out.append(CheckResult(
            "Shioaji API 金鑰",
            "ok",
            f"已設定 (API_KEY 前 6 碼: {settings.api_key[:6]}…)",
        ))
    else:
        miss = []
        if not settings.api_key: miss.append("API_KEY")
        if not settings.secret_key: miss.append("SECRET_KEY")
        out.append(CheckResult(
            "Shioaji API 金鑰", "fail",
            f"缺 {' / '.join(miss)}",
            "到 .env 設定，或在儀表板「組態設定」頁填入後存檔",
        ))

    # 當沖封存
    if day_trading_trade_blocked(settings):
        out.append(CheckResult(
            "當沖模組封存", "fail",
            "RUN_MODE=trade 已封存，stock-bot 啟動會被阻擋",
            "改用 watch/report，或設 DAY_TRADING_UNFREEZE=true（見 docs/archive/day-trading.md）",
        ))
    elif getattr(settings, "day_trading_archived", True) and settings.run_mode == "trade":
        out.append(CheckResult(
            "當沖模組封存", "warn",
            "已解除封存 (DAY_TRADING_UNFREEZE=true)，使用舊版當沖邏輯",
            "新功能請改走微笑曲線策略（規劃中）",
        ))
    else:
        out.append(CheckResult(
            "當沖模組封存", "info",
            "當沖實單未啟用或已解除封存",
        ))

    # RUN_MODE
    if settings.run_mode == "trade":
        out.append(CheckResult(
            "執行模式 (RUN_MODE)", "ok",
            "trade — 啟動後會實際下單 (依 strategy 觸發)",
        ))
    elif settings.run_mode == "watch":
        out.append(CheckResult(
            "執行模式 (RUN_MODE)", "warn",
            "watch — 只訂閱行情並記錄訊號，不會下單",
            "當沖實單已封存；研究分析請維持 watch/report",
        ))
    elif settings.run_mode == "report":
        out.append(CheckResult(
            "執行模式 (RUN_MODE)", "info",
            "report — 純報表彙整模式，不連線、不下單",
        ))
    else:
        out.append(CheckResult(
            "執行模式 (RUN_MODE)", "fail",
            f"未知模式: {settings.run_mode}",
            "設定為 trade / watch / report 之一",
        ))

    # SIMULATION
    if settings.simulation:
        out.append(CheckResult(
            "模擬模式 (SIMULATION)", "warn",
            "true — 連到永豐模擬環境，不會動到真錢",
            "確認過策略沒問題後，把 SIMULATION 設成 false 才會真實下單",
        ))
    else:
        out.append(CheckResult(
            "模擬模式 (SIMULATION)", "ok",
            "false — 接到正式環境，下單會真實成交",
        ))

    # 監控池（手動 SYMBOLS + 四源合併後的最終清單）
    auto_merge = bool(getattr(settings, "symbols_auto_merge", True))
    pool_label = "監控池 (四源合併)" if auto_merge else "監控標的 (SYMBOLS)"
    if settings.symbols:
        out.append(CheckResult(
            pool_label,
            "ok",
            _format_watch_pool_detail(
                list(settings.symbols),
                auto_merge=auto_merge,
                source_counts=watch_pool_source_counts,
            ),
            None if auto_merge else "僅 .env SYMBOLS；設 SYMBOLS_AUTO_MERGE=true 可合併戰情室/明日關注",
        ))
    else:
        out.append(CheckResult(
            pool_label,
            "warn",
            "監控池為空 — trade 模式至少要 1 檔",
            "設 SYMBOLS 或啟用 SYMBOLS_AUTO_MERGE 並確保戰情室/明日關注報告存在",
        ))

    return out


def _ca_skip_reason(settings: Settings) -> str:
    """說明目前設定下 broker 為何不啟用 CA。"""
    if settings.run_mode != "trade":
        return f"RUN_MODE={settings.run_mode} — 只接行情/報表，不啟用 CA"
    if settings.simulation:
        return "SIMULATION=true — 模擬環境不啟用 CA"
    return ""


def _should_test_ca_activation(
    settings: Settings,
    *,
    test_ca_activate: bool = False,
) -> bool:
    """是否應在 preflight 連線階段呼叫 activate_ca。"""
    if not settings.ca_path:
        return False
    if settings.run_mode == "trade" and not settings.simulation:
        return True
    return bool(test_ca_activate and not settings.simulation)


def _check_ca(settings: Settings) -> List[CheckResult]:
    """電子憑證只在 trade + simulation=false 時必要。其餘只算 info/warn。"""
    out: List[CheckResult] = []
    need_ca = settings.run_mode == "trade" and not settings.simulation

    # CA 檔
    if not settings.ca_path:
        out.append(CheckResult(
            "電子憑證檔案", "fail" if need_ca else "info",
            "CA_PATH 未設定",
            "下載 Sinopac 電子憑證並設 CA_PATH=D:/path/to/Sinopac.pfx" if need_ca else "",
        ))
    else:
        p = Path(settings.ca_path).expanduser()
        if not p.exists():
            out.append(CheckResult(
                "電子憑證檔案", "fail" if need_ca else "warn",
                f"檔案不存在: {p}",
                "確認 CA_PATH 路徑、或重新從永豐下載憑證",
            ))
        else:
            size = p.stat().st_size
            out.append(CheckResult(
                "電子憑證檔案", "ok",
                f"{p} ({size} bytes)",
            ))
            # 試讀過期日 (pypdf 用不到，這裡用 cryptography 試讀 PFX)
            exp_info = _read_pfx_expiry(p, settings.ca_password)
            if exp_info:
                today = now_tw().date()
                days_left = (exp_info - today).days
                if days_left <= 0:
                    out.append(CheckResult(
                        "電子憑證有效期", "fail",
                        f"已於 {exp_info} 過期",
                        "重新下載新憑證",
                    ))
                elif days_left <= 30:
                    out.append(CheckResult(
                        "電子憑證有效期", "warn",
                        f"還剩 {days_left} 天 ({exp_info})",
                        "建議盡早續發",
                    ))
                else:
                    out.append(CheckResult(
                        "電子憑證有效期", "ok",
                        f"還剩 {days_left} 天 ({exp_info})",
                    ))

    # CA 密碼 + 身分證
    if need_ca:
        if not settings.ca_password:
            out.append(CheckResult(
                "電子憑證密碼", "fail",
                "CA_PASSWORD 未設定",
                "在 .env 填入憑證密碼 (預設多半是身分證字號)",
            ))
        else:
            out.append(CheckResult(
                "電子憑證密碼", "ok", "已設定 (隱藏)",
            ))
        if not settings.person_id:
            out.append(CheckResult(
                "身分證字號", "fail",
                "PERSON_ID 未設定",
                "在 .env 設 PERSON_ID=A12...",
            ))
        else:
            mask = settings.person_id[:1] + "*" * (len(settings.person_id) - 2) + settings.person_id[-1:]
            out.append(CheckResult(
                "身分證字號 (PERSON_ID)", "ok", f"已設定 ({mask})",
            ))
    elif settings.ca_path:
        reason = _ca_skip_reason(settings)
        out.append(CheckResult(
            "電子憑證啟用條件", "info",
            f"目前 {reason}，CA 暫不啟用",
            "若要實單下單請設 RUN_MODE=trade 且 SIMULATION=false；"
            "或勾選「測試 CA 啟用」驗證憑證",
        ))

    return out


def _read_pfx_expiry(path: Path, password: str) -> Optional[dt.date]:
    """嘗試讀 PFX 內憑證的過期日 (失敗時回 None)。"""
    try:
        from cryptography.hazmat.primitives.serialization import pkcs12
        data = path.read_bytes()
        passwd_bytes = password.encode("utf-8") if password else None
        _, cert, _ = pkcs12.load_key_and_certificates(data, passwd_bytes)
        if cert is None:
            return None
        # not_valid_after_utc 是 cryptography 42+ 的 API
        try:
            return cert.not_valid_after_utc.date()
        except AttributeError:
            return cert.not_valid_after.date()
    except ImportError:
        return None
    except Exception:
        return None


def _check_login(
    settings: Settings,
    *,
    do_real_login: bool,
    logger: logging.Logger,
) -> tuple[List[CheckResult], Optional[Any], Optional[Any]]:
    """嘗試實際登入 Shioaji。回傳 (results, api_obj, stock_account)。"""
    out: List[CheckResult] = []
    if not settings.api_key or not settings.secret_key:
        out.append(CheckResult(
            "Shioaji 登入", "fail",
            "缺 API_KEY / SECRET_KEY — 跳過實際連線測試",
        ))
        return out, None, None
    if not do_real_login:
        out.append(CheckResult(
            "Shioaji 登入", "info",
            "do_real_login=False — 略過連線測試",
        ))
        return out, None, None

    try:
        import shioaji as sj
    except ImportError:
        out.append(CheckResult(
            "Shioaji 套件", "fail",
            "shioaji 未安裝", "uv add shioaji",
        ))
        return out, None, None

    api = None
    try:
        api = sj.Shioaji(simulation=settings.simulation)
        accounts = api.login(
            api_key=settings.api_key,
            secret_key=settings.secret_key,
            contracts_timeout=10_000,
        )
        out.append(CheckResult(
            "Shioaji 登入", "ok",
            f"模擬={settings.simulation}；可用帳號 {len(accounts) if accounts else 0} 個",
            extra={"accounts_count": len(accounts) if accounts else 0},
        ))
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "not allow" in low and "ip" in low:
            # IP 白名單阻擋：金鑰綁定的允許 IP 不含目前對外 IP。與程式碼無關。
            import re as _re
            m = _re.search(r"ip:\s*([0-9a-fA-F:.]+)", msg)
            bad_ip = m.group(1) if m else "你目前的對外 IP"
            out.append(CheckResult(
                "Shioaji 登入", "fail",
                f"IP 白名單阻擋：{bad_ip} 不在這把 API 金鑰允許的 IP 清單內 (status 400)",
                f"到永豐 iLeader → API 金鑰管理，編輯這把金鑰的「IP 限制」："
                f"家用浮動 IP 建議直接「不綁定 IP / 移除限制」，"
                f"或把 {bad_ip} 加入允許清單。這是金鑰設定問題，非程式碼問題。",
                extra={"reason": "ip_whitelist", "blocked_ip": bad_ip},
            ))
        elif "permission" in low or "401" in low:
            out.append(CheckResult(
                "Shioaji 登入", "fail",
                f"API 金鑰權限不足: {msg[:120]}",
                "到永豐 iLeader → API 金鑰管理：勾選「下單」權限後重新產生金鑰",
                extra={"reason": "no_trade_permission"},
            ))
        else:
            out.append(CheckResult(
                "Shioaji 登入", "fail",
                f"登入失敗: {e}",
                "檢查 API_KEY / SECRET_KEY；若是真實環境需先在永豐 iLeader 開通 Shioaji",
            ))
        return out, api, None

    stock_account = getattr(api, "stock_account", None)
    if stock_account is None:
        out.append(CheckResult(
            "證券帳戶", "fail",
            "登入成功但拿不到 stock_account",
            "可能 API key 未開通證券下單權限",
        ))
        return out, api, None

    signed = bool(getattr(stock_account, "signed", False))
    broker_id = getattr(stock_account, "broker_id", "")
    account_id = getattr(stock_account, "account_id", "")
    username = getattr(stock_account, "username", "")
    masked_acc = account_id[:2] + "*" * max(0, len(account_id) - 4) + account_id[-2:] if account_id else ""
    out.append(CheckResult(
        "證券帳戶 (stock_account)", "ok" if signed else "warn",
        f"broker={broker_id} account={masked_acc} signed={signed}"
        + (f" ({username})" if username else ""),
        "signed=False 代表線上簽署協議尚未完成；登入永豐 iLeader 簽完即可" if not signed else "",
        extra={
            "broker_id": broker_id,
            "account_id_masked": masked_acc,
            "signed": signed,
            "username": username,
        },
    ))

    return out, api, stock_account


def _check_account(
    settings: Settings,
    api: Any,
    stock_account: Any,
    *,
    logger: logging.Logger,
    test_ca_activate: bool = False,
) -> List[CheckResult]:
    out: List[CheckResult] = []
    if api is None or stock_account is None:
        return out

    if _should_test_ca_activation(settings, test_ca_activate=test_ca_activate):
        ca_path = Path(settings.ca_path).expanduser()
        if not ca_path.exists():
            out.append(CheckResult(
                "電子憑證啟用", "fail",
                f"activate_ca 略過：檔案不存在 ({ca_path})",
                "確認 CA_PATH 路徑",
            ))
        else:
            try:
                api.activate_ca(
                    ca_path=settings.ca_path,
                    ca_passwd=settings.ca_password,
                    person_id=settings.person_id,
                )
                detail = "activate_ca 成功"
                if test_ca_activate and settings.run_mode != "trade":
                    detail += f"（額外測試；RUN_MODE={settings.run_mode} 平常不會啟用 CA）"
                out.append(CheckResult(
                    "電子憑證啟用", "ok", detail,
                ))
            except Exception as e:
                out.append(CheckResult(
                    "電子憑證啟用", "fail",
                    f"activate_ca 失敗: {e}",
                    "確認 CA_PASSWORD 與 PERSON_ID；CA 是不是用同一個身分證申請的",
                ))

    # 商品檔 — 模擬環境不會有完整檔，info 即可
    try:
        api.fetch_contracts(contract_download=True)
        stocks = api.Contracts.Stocks
        c = (
            stocks.get("2330")
            or stocks.TSE.get("2330")
            or stocks.OTC.get("2330")
        )
        if c is None:
            try:
                c = stocks["2330"]
            except Exception:
                c = None
        if c is not None:
            out.append(CheckResult(
                "商品檔 (Contracts)", "ok",
                f"2330 {c.name} 漲停 {c.limit_up} 跌停 {c.limit_down}",
            ))
        else:
            out.append(CheckResult(
                "商品檔 (Contracts)", "info",
                "2330 取不到 — 模擬環境通常沒完整商品檔，下單時才會抓",
            ))
    except Exception as e:
        out.append(CheckResult(
            "商品檔 (Contracts)", "info",
            f"取商品檔失敗 (模擬環境常見): {e}",
        ))

    # 餘額
    bal_amount: Optional[float] = None
    try:
        balance = api.account_balance()
        if balance:
            bal_amount = float(getattr(balance, "acc_balance", 0) or 0)
            out.append(CheckResult(
                "帳戶餘額 (account_balance)", "ok",
                f"可用餘額 {bal_amount:,.0f} TWD",
                extra={"balance": bal_amount},
            ))
    except Exception as e:
        out.append(CheckResult(
            "帳戶餘額", "info",
            f"無法取得 (Shioaji 模擬環境不支援): {e}",
        ))

    # 部位 & 下單權限總判斷 (Token 沒 Trade 權限時這支會 401)
    has_trade_permission = True
    permission_detail = ""
    try:
        positions = api.list_positions(stock_account)
        if positions:
            out.append(CheckResult(
                "現有部位", "ok",
                f"目前 {len(positions)} 檔持倉",
                extra={"positions": [
                    {"code": getattr(p, "code", ""), "qty": getattr(p, "quantity", 0)}
                    for p in positions[:20]
                ]},
            ))
        else:
            out.append(CheckResult(
                "現有部位", "info", "目前無持倉",
            ))
    except Exception as e:
        msg = str(e)
        if "permission" in msg.lower() or "401" in msg:
            has_trade_permission = False
            permission_detail = msg
        else:
            out.append(CheckResult(
                "現有部位", "info",
                f"無法取得: {e}",
            ))

    # ★ 重要：API Token 是否含 Trade 權限
    if has_trade_permission:
        out.append(CheckResult(
            "API Token 下單權限", "ok",
            "可呼叫 list_positions → 含 Trade 權限",
        ))
    else:
        out.append(CheckResult(
            "API Token 下單權限", "fail",
            f"Token 僅有 Data 權限，無法下單 ({permission_detail[:80]})",
            "到永豐 iLeader → API 金鑰管理：勾選「下單」權限後重新產生 API Key/Secret",
        ))

    return out


def _check_risk(settings: Settings) -> List[CheckResult]:
    out: List[CheckResult] = []

    # 停損
    if not (-20 < settings.stop_loss_pct < 0):
        out.append(CheckResult(
            "停損 STOP_LOSS_PCT", "warn",
            f"{settings.stop_loss_pct}% (建議 -1% ~ -8% 之間)",
            "用儀表板「組態設定」調整",
        ))
    else:
        out.append(CheckResult(
            "停損 STOP_LOSS_PCT", "ok",
            f"{settings.stop_loss_pct}%",
        ))

    # 停利
    if not (0 < settings.take_profit_pct < 50):
        out.append(CheckResult(
            "停利 TAKE_PROFIT_PCT", "warn",
            f"{settings.take_profit_pct}%",
        ))
    else:
        out.append(CheckResult(
            "停利 TAKE_PROFIT_PCT", "ok",
            f"+{settings.take_profit_pct}%",
        ))

    # 移動停利
    out.append(CheckResult(
        "移動停利 TRAILING_STOP_PCT", "ok" if settings.trailing_stop_pct >= 0 else "warn",
        f"{settings.trailing_stop_pct}% 回撤觸發",
    ))

    # 風控
    if settings.max_fund <= 0:
        out.append(CheckResult(
            "最大下單資金 MAX_FUND", "fail",
            f"{settings.max_fund} — 設成 0 會無法下單",
        ))
    else:
        rrr = abs(settings.take_profit_pct / settings.stop_loss_pct) if settings.stop_loss_pct else 0
        out.append(CheckResult(
            "最大下單資金 MAX_FUND", "ok",
            f"{settings.max_fund:,} TWD ｜ 預期盈虧比 {rrr:.2f}",
        ))

    if settings.max_lot_per_symbol < 1:
        out.append(CheckResult(
            "單檔最大張數 MAX_LOT_PER_SYMBOL", "fail",
            f"{settings.max_lot_per_symbol}",
            "至少設成 1 才能下單",
        ))
    else:
        out.append(CheckResult(
            "單檔最大張數 MAX_LOT_PER_SYMBOL", "ok",
            f"{settings.max_lot_per_symbol} 張 / 檔",
        ))

    return out


def _check_time(settings: Settings) -> List[CheckResult]:
    """檢查現在是否在台股交易時間。"""
    out: List[CheckResult] = []
    now = now_tw()
    weekday = now.weekday()  # 0=Mon
    is_weekend = weekday >= 5
    today = now.date()
    current_time = now.time()

    # 交易日 (週末粗判 — 國定假日要再查)
    if is_weekend:
        out.append(CheckResult(
            "今日是否為交易日", "warn",
            f"{today} 是週{['一','二','三','四','五','六','日'][weekday]} — 週末不開市",
            "保留設定，等下一個交易日 09:00",
        ))
    else:
        out.append(CheckResult(
            "今日是否為交易日", "ok",
            f"{today} 週{['一','二','三','四','五','六','日'][weekday]}",
        ))

    # 盤中時間 (09:00-13:30)
    market_open = dt.time(9, 0)
    market_close = dt.time(13, 30)
    if is_weekend:
        in_session = False
    else:
        in_session = market_open <= current_time <= market_close

    if in_session:
        # 距收盤多久
        close_dt = dt.datetime.combine(today, market_close, tzinfo=now.tzinfo)
        mins_left = int((close_dt - now).total_seconds() // 60)
        out.append(CheckResult(
            "現在是否在盤中", "ok",
            f"是 — 現在 {current_time.strftime('%H:%M')}，距收盤 {mins_left} 分鐘",
        ))
    else:
        out.append(CheckResult(
            "現在是否在盤中", "warn",
            f"否 — 現在 {current_time.strftime('%H:%M')} (盤中 09:00-13:30)",
            "盤外送單只能用「IOC/ROD pending」且通常被退單",
        ))

    # 進場/出場時間
    out.append(CheckResult(
        "進場截止時間 (ENTER_CUTOFF_TIME)", "info",
        f"{settings.enter_cutoff_time.strftime('%H:%M')} "
        f"(configurable 策略以 EXIT_TIME {settings.exit_time.strftime('%H:%M')} 為進場截止)",
    ))
    profit_start = getattr(settings, "profit_exit_start_time", None)
    if profit_start is not None:
        out.append(CheckResult(
            "午盤獲利平倉 (PROFIT_EXIT_START_TIME)", "info",
            f"{profit_start.strftime('%H:%M')}–{settings.exit_time.strftime('%H:%M')} "
            f"淨利 > 0 即賣出，{settings.exit_time.strftime('%H:%M')} 強制清倉剩餘",
        ))
    out.append(CheckResult(
        "強制平倉時間 (EXIT_TIME)", "info",
        f"{settings.exit_time.strftime('%H:%M')} 強制將當沖部位平掉",
    ))

    return out


def _check_position_safety(
    settings: Settings,
    *,
    do_real_login: bool = True,
) -> List[CheckResult]:
    """監控標的 vs 手動持股重疊、本工具紀錄對帳。"""
    from bot.position_safety import (
        run_startup_safety_checks,
        symbols_to_exclude_for_trading,
    )

    out: List[CheckResult] = []
    excluded = symbols_to_exclude_for_trading(settings)
    if excluded:
        out.append(CheckResult(
            "手動持股 / 黑名單排除",
            "info",
            f"以下代號不會自動交易: {', '.join(excluded)}",
        ))

    monitored = [s for s in (settings.symbols or []) if s not in set(excluded)]
    if not monitored:
        out.append(CheckResult(
            "監控標的清單",
            "warn",
            "SYMBOLS 為空或全數被黑名單/手動持股排除",
            "確認 SYMBOLS 與 MANUAL_HOLD_SYMBOLS 設定",
        ))
        return out

    if not do_real_login or not settings.api_key:
        out.append(CheckResult(
            "券商庫存重疊檢查",
            "info",
            "略過（未執行真實登入）；啟動 bot 時仍會做對帳",
        ))
        return out

    report = run_startup_safety_checks(
        settings,
        engage_kill_switch=False,
        require_broker_snapshot=True,
    )

    if report.broker_fetch_error:
        out.append(CheckResult(
            "券商庫存讀取",
            "fail",
            report.broker_fetch_error,
            "確認 API 連線與下單權限；trade 模式啟動時亦會 fail-closed",
        ))
        return out

    if report.ok:
        out.append(CheckResult(
            "券商庫存 vs 本工具紀錄",
            "ok",
            f"監控 {len(monitored)} 檔無手動持股重疊、對帳一致",
        ))
        return out

    for issue in report.overlap_issues:
        out.append(CheckResult(
            f"手動持股重疊 [{issue.symbol}]",
            "fail",
            issue.message,
            "將該檔移出 SYMBOLS，或加入 MANUAL_HOLD_SYMBOLS / BLACKLIST_SYMBOLS",
            extra={
                "broker_qty": issue.broker_qty,
                "bot_qty": issue.bot_qty,
                "manual_qty": issue.manual_qty,
            },
        ))
    for issue in report.reconcile_issues:
        out.append(CheckResult(
            f"對帳異常 [{issue.symbol}]",
            "fail",
            issue.message,
            "確認 data/trades_*.csv 與券商庫存；必要時手動平倉後再啟動",
            extra={"kind": issue.kind},
        ))
    return out


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------


def run_preflight(
    settings: Optional[Settings] = None,
    *,
    do_real_login: bool = True,
    test_ca_activate: bool = False,
    logger: Optional[logging.Logger] = None,
) -> PreflightReport:
    """跑全部檢查，回傳結構化報告。

    do_real_login=False 可用於 CI / unit test，避免真的打 Shioaji。
    """
    log = logger or get_logger("preflight")
    settings = settings or Settings()

    watch_pool_source_counts: Optional[Dict[str, int]] = None
    if getattr(settings, "symbols_auto_merge", True):
        from pathlib import Path

        from bot.watch_symbol_pool import merge_watch_symbols_into_settings

        pool_result = merge_watch_symbols_into_settings(
            settings,
            Path.cwd(),
            reason="preflight",
            logger=log,
        )
        watch_pool_source_counts = pool_result.source_counts

    report = PreflightReport(
        fetched_at=now_tw().isoformat(timespec="seconds"),
    )

    log.info("=== Preflight 開始 ===")

    report.section_env = _check_env(
        settings,
        watch_pool_source_counts=watch_pool_source_counts,
    )
    report.section_ca = _check_ca(settings)
    report.section_login, api, stock_account = _check_login(
        settings, do_real_login=do_real_login, logger=log,
    )
    report.section_account = _check_account(
        settings, api, stock_account, logger=log, test_ca_activate=test_ca_activate,
    )
    report.section_risk = _check_risk(settings)
    report.section_time = _check_time(settings)
    report.section_safety = _check_position_safety(settings, do_real_login=do_real_login)

    # ---- 最終結論 ----
    report.can_simulate = not report.has_fail()  # 模擬只需設定齊全 + 登入成功
    report.can_trade_now = (
        report.can_simulate
        and not day_trading_trade_blocked(settings)
        and not settings.simulation
        and settings.run_mode == "trade"
        and any(
            c.name == "現在是否在盤中" and c.status == "ok"
            for c in report.section_time
        )
        and any(
            c.name.startswith("證券帳戶") and c.status == "ok"
            for c in report.section_login
        )
    )

    # 文字摘要
    bits: List[str] = []
    if report.has_fail():
        fails = [c.name for c in report.all_checks() if c.status == "fail"]
        bits.append(f"❌ 阻擋項: {', '.join(fails[:5])}")
    elif report.has_warn():
        warns = [c.name for c in report.all_checks() if c.status == "warn"]
        bits.append(f"⚠️ 注意項: {', '.join(warns[:5])}")
    else:
        bits.append("✅ 所有項目通過")

    if report.can_trade_now:
        bits.append("🟢 現在可以實際下台股單")
    elif report.can_simulate:
        if settings.simulation:
            bits.append("🟡 目前 simulation=true — 僅能模擬下單；切到正式環境前請先 dry-run 過策略")
        elif settings.run_mode != "trade":
            bits.append(f"🟡 RUN_MODE={settings.run_mode} — 不會下單；要下單請改 trade")
        else:
            bits.append("🟡 非盤中時間或帳號未完整啟用 — 暫無法立即下單")
    else:
        bits.append("🔴 阻擋條件未解決，無法下單 (模擬與正式都不行)")

    report.summary = " ｜ ".join(bits)

    # 關閉登入 (留下來會佔 session)
    try:
        if api is not None:
            api.logout()
    except Exception:
        pass

    log.info("=== Preflight 完成: %s ===", report.summary)
    return report


def report_to_dict(r: PreflightReport) -> Dict[str, Any]:
    return {
        "fetched_at": r.fetched_at,
        "can_trade_now": r.can_trade_now,
        "can_simulate": r.can_simulate,
        "summary": r.summary,
        "sections": {
            "env": [asdict(c) for c in r.section_env],
            "ca": [asdict(c) for c in r.section_ca],
            "login": [asdict(c) for c in r.section_login],
            "account": [asdict(c) for c in r.section_account],
            "risk": [asdict(c) for c in r.section_risk],
            "time": [asdict(c) for c in r.section_time],
            "safety": [asdict(c) for c in r.section_safety],
        },
    }


__all__ = [
    "CheckResult",
    "PreflightReport",
    "run_preflight",
    "report_to_dict",
]

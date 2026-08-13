"""stock-preflight CLI -- 一鍵跑交易可行性檢查。

用法
====
    uv run stock-preflight                    # 包含真實 Shioaji 登入測試
    uv run stock-preflight --no-login         # 只檢查設定，不打 Shioaji
    uv run stock-preflight --test-ca          # watch 模式也測試 activate_ca
    uv run stock-preflight --json             # 輸出 JSON (給 CI 用)
"""

from __future__ import annotations

import argparse
import json
import sys

from bot.config import Settings
from bot.preflight import report_to_dict, run_preflight
from bot.utils import get_logger

_STATUS_ICON = {
    "ok": "✅",
    "warn": "⚠️",
    "fail": "❌",
    "info": "ℹ️",
}

_SECTION_TITLES = {
    "section_env": "1. 環境變數",
    "section_ca": "2. 電子憑證 (CA)",
    "section_login": "3. Shioaji 連線",
    "section_account": "4. 帳戶權限",
    "section_risk": "5. 風控設定",
    "section_time": "6. 時間窗口",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-preflight",
        description="檢查現在是否能下台股單 (含真實 Shioaji 登入測試)",
    )
    parser.add_argument(
        "--no-login", action="store_true",
        help="略過真實 Shioaji 連線測試 (offline mode)",
    )
    parser.add_argument(
        "--test-ca", action="store_true",
        help="即使 RUN_MODE=watch 也測試 activate_ca（需搭配連線測試）",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="輸出 JSON 格式 (給 CI / Telegram 用)",
    )
    args = parser.parse_args(argv)

    # Windows console UTF-8 強制
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    logger = get_logger("stock-preflight")
    settings = Settings()
    report = run_preflight(
        settings,
        do_real_login=not args.no_login,
        test_ca_activate=args.test_ca,
        logger=logger,
    )

    if args.json:
        print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2))
        return 0 if report.can_simulate else 1

    # 人類可讀版
    print()
    print(f"=== Preflight 報告 ({report.fetched_at}) ===")
    print(f"  {report.summary}")
    print()
    for attr, title in _SECTION_TITLES.items():
        items = getattr(report, attr, [])
        if not items:
            continue
        print(f"--- {title} ---")
        for c in items:
            icon = _STATUS_ICON.get(c.status, " ")
            print(f"  {icon} {c.name}")
            if c.detail:
                print(f"      └ {c.detail}")
            if c.suggestion:
                print(f"      💡 {c.suggestion}")
        print()

    print(f"最終結論: {'🟢 可下真實單' if report.can_trade_now else '🟡 暫不可下真實單' if report.can_simulate else '🔴 設定有阻擋'}")
    return 0 if not report.has_fail() else 2


if __name__ == "__main__":
    sys.exit(main())

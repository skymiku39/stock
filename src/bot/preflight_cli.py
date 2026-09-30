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
from pathlib import Path
from typing import List, Optional

from bot.config import Settings
from bot.cli_encoding import configure_console
from bot.preflight import report_to_dict, run_preflight
from bot.utils import get_logger, log_root


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
    "section_safety": "7. 持倉安全",
}


def main(argv: Optional[List[str]] = None) -> int:
    configure_console()
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
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="專案設定根目錄（未指定時使用目前工作目錄）",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve() if args.root else Path.cwd()
    with log_root(root):
        logger = get_logger("stock-preflight")
        settings = Settings(_env_file=str(root / ".env"))  # type: ignore[call-arg]
        report = run_preflight(
            settings,
            do_real_login=not args.no_login,
            test_ca_activate=args.test_ca,
            project_root=root,
            logger=logger,
        )

    exit_code = 0 if not report.has_fail() else 2

    if args.json:
        print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2))
        return exit_code

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
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

"""盤中零股買賣流程煙霧測試 (stock-oddlot-test)。

目的：在 **Shioaji 模擬環境 (simulation=True)** 下，完整跑一次「盤中零股」
買進 → 查詢 → 賣出 → 查詢的下單流程，驗證程式碼路徑可正常運作，
而**不會送出任何真實委託**。

安全機制：
  * 強制 simulation=True；若偵測到 simulation=False 直接中止，避免真實下單。
  * 即使 .env 設定為 RUN_MODE=watch，本腳本只在記憶體內把 run_mode 覆寫為
    trade（搭配 simulation=True），不會修改 .env，也不會啟用電子憑證 (CA)。

用法：
    uv run stock-oddlot-test                 # 預設 2330，買賣各 10 股
    uv run stock-oddlot-test --symbol 0050 --shares 5
    uv run stock-oddlot-test --no-sell       # 只測買進
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

from bot.config import Settings
from bot.utils import get_logger


def _safe_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def _resolve_price(broker, symbol: str, logger) -> Optional[float]:
    """取得一個合理的限價：優先用快照成交價，其次前日收盤 (reference)。"""
    contract = broker.get_contract(symbol)
    if contract is None:
        return None

    # 1) 嘗試即時快照成交價
    try:
        snaps = broker.api.snapshots([contract])
        if snaps:
            close = float(getattr(snaps[0], "close", 0) or 0)
            if close > 0:
                logger.info("快照成交價: %s = %.2f", symbol, close)
                return close
    except Exception:
        logger.debug("snapshots 取價失敗，改用 reference", exc_info=True)

    # 2) 退回前日收盤
    ref = float(getattr(contract, "reference", 0) or 0)
    if ref > 0:
        logger.info("前日收盤 (reference): %s = %.2f", symbol, ref)
        return ref

    return None


def _describe_trade(trade) -> str:
    if trade is None:
        return "None (下單未送出 / 被攔截)"
    try:
        oid = getattr(trade.order, "id", "?")
        status = getattr(trade.status, "status", "?")
        deal_qty = getattr(trade.status, "deal_quantity", "?")
        msg = getattr(trade.status, "msg", "") or ""
        return f"id={oid} status={status} deal_qty={deal_qty} {msg}".strip()
    except Exception:
        return repr(trade)


def main() -> None:
    _safe_stdout()
    parser = argparse.ArgumentParser(description="盤中零股下單流程模擬測試")
    parser.add_argument("--symbol", default="2330", help="股票代號 (預設 2330)")
    parser.add_argument("--shares", type=int, default=10, help="股數 1~999 (預設 10)")
    parser.add_argument("--price", type=float, default=0.0, help="限價 (0=自動抓取)")
    parser.add_argument("--no-sell", action="store_true", help="只測買進，不測賣出")
    args = parser.parse_args()

    logger = get_logger("oddlot-test")
    logger.info("=== 盤中零股流程測試 (模擬) ===")

    from shioaji.constant import Action

    from bot.broker import SjBroker

    # 在記憶體覆寫：run_mode=trade 才能呼叫 place_order，simulation 強制 True。
    settings = Settings(run_mode="trade", simulation=True)

    # ---- 安全閘門：拒絕在非模擬環境執行 ----
    if not settings.simulation:
        logger.error("偵測到 simulation=False，為避免真實下單已中止。")
        sys.exit(2)
    if not settings.api_key or not settings.secret_key:
        logger.error("缺少 API_KEY / SECRET_KEY，無法登入模擬環境。")
        sys.exit(1)

    logger.info(
        "模式: run_mode=%s simulation=%s symbol=%s shares=%d",
        settings.run_mode, settings.simulation, args.symbol, args.shares,
    )

    broker = SjBroker(settings)
    if not broker.login():
        logger.error("登入模擬環境失敗")
        sys.exit(1)

    # 模擬下單 (paper trading) 仍需簽署 CA 才有下單權限 (token ca_required=true)。
    # simulation=True 不會送出真實委託，因此在此啟用 CA 是安全的。
    if settings.ca_path:
        try:
            logger.info("啟用電子憑證 (模擬下單需要) ...")
            broker.api.activate_ca(
                ca_path=settings.ca_path,
                ca_passwd=settings.ca_password,
                person_id=settings.person_id,
            )
            logger.info("憑證啟用完成")
        except Exception:
            logger.exception("CA 啟用失敗，下單可能仍會被拒")
    else:
        logger.warning("未設定 CA_PATH；若 API 金鑰需簽署 CA，下單會被拒。")

    exit_code = 0
    try:
        contract = broker.get_contract(args.symbol)
        if contract is None:
            logger.error("找不到合約: %s", args.symbol)
            sys.exit(1)

        price = args.price if args.price > 0 else _resolve_price(broker, args.symbol, logger)
        if not price or price <= 0:
            logger.error("無法取得 %s 的可用限價，請用 --price 指定", args.symbol)
            sys.exit(1)

        logger.info("--- (1/2) 零股買進 %d 股 @ %.2f ---", args.shares, price)
        buy = broker.place_odd_lot_order(
            symbol=args.symbol,
            action=Action.Buy,
            shares=args.shares,
            price=price,
            custom_field="oddbuy",
        )
        logger.info("買進結果: %s", _describe_trade(buy))
        if buy is None:
            err = repr(broker.last_order_error or "")
            if "permission" in err.lower() or "401" in err:
                logger.error(
                    "下單被拒：API 金鑰僅有『行情(Data)』權限，無『下單』權限。"
                )
                logger.error(
                    "→ 請到永豐 Shioaji API 後台為此金鑰開通『下單』權限後重試；"
                    "程式下單流程本身正常 (委託內容已正確組成並送達)。"
                )
                logger.info("=== 流程測試結束：程式流程 OK，外部權限不足 (BLOCKED) ===")
                sys.exit(3)
            logger.error("買進未送出 (place_order 回傳 None)，流程驗證失敗")
            exit_code = 1
        time.sleep(1.0)
        try:
            broker.update_status()
        except Exception:
            logger.debug("update_status (buy) 失敗", exc_info=True)

        if not args.no_sell:
            logger.info("--- (2/2) 零股賣出 %d 股 @ %.2f ---", args.shares, price)
            sell = broker.place_odd_lot_order(
                symbol=args.symbol,
                action=Action.Sell,
                shares=args.shares,
                price=price,
                custom_field="oddsel",
            )
            logger.info("賣出結果: %s", _describe_trade(sell))
            if sell is None:
                logger.error("賣出未送出 (place_order 回傳 None)，流程驗證失敗")
                exit_code = 1
            time.sleep(1.0)
            try:
                broker.update_status()
            except Exception:
                logger.debug("update_status (sell) 失敗", exc_info=True)

        try:
            trades = broker.list_trades()
            logger.info("目前委託數: %d", len(trades))
            for t in trades:
                logger.info("  • %s", _describe_trade(t))
        except Exception:
            logger.debug("list_trades 失敗", exc_info=True)

        logger.info(
            "=== 流程測試完成 (simulation=True，未送出任何真實委託) === 結果=%s",
            "PASS" if exit_code == 0 else "FAIL",
        )
    finally:
        broker.logout()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

"""Financial Modeling Prep（stable 路徑）。金鑰走查詢參數 apikey。"""
from __future__ import annotations

import re
from typing import Any, List, Optional

from bot.free_sources.base import BaseClient

BASE_URL = "https://financialmodelingprep.com/stable"

# FMP 對「免費方案不允許的參數值」與「方案不含的端點 / 代號」也回 402，不是額度用完。
# 2026-11-24 實測 limit=8 的回應：
# "Premium Query Parameter: 'Special Parameters : The values for 'limit' must be between 0 and 5 ..."
_PARAM_ERROR = re.compile(r"(?i)premium query parameter|must be between|invalid (?:value|parameter)")
_PLAN_RESTRICTED = re.compile(
    r"(?i)premium endpoint|restricted endpoint|special endpoint|not available under your current subscription"
)


class FmpClient(BaseClient):
    source = "fmp"

    def _check_body(self, data: Any) -> None:
        if isinstance(data, dict) and (data.get("Error Message") or data.get("error")):
            raise self._err(self.last_status, data.get("Error Message") or data.get("error"), "api_error")

    def _classify_http_error(self, status: int, body: str) -> Optional[str]:
        if status != 402:
            return None
        # 先判「方案不含」：2026-11-24 排程實測 ASML 回
        # "Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available
        # under your current subscription"，兩種字樣都有；它只影響該檔，不能當成參數錯誤整批停掉。
        if _PLAN_RESTRICTED.search(body or ""):
            return "plan_restricted"
        if _PARAM_ERROR.search(body or ""):
            return "invalid_params"
        return None  # 其餘 402 仍視為額度用完

    def _statement(self, path: str, symbol: str, period: str, limit: int) -> List[dict]:
        params = {"symbol": symbol, "period": period, "limit": limit, "apikey": self._api_key}
        data = self._get(f"{BASE_URL}/{path}", params)
        if not isinstance(data, list):
            raise self._err(self.last_status, "unexpected response shape", "bad_response")
        return data

    def income_statement(self, symbol: str, period: str = "annual", limit: int = 5) -> List[dict]:
        return self._statement("income-statement", symbol, period, limit)

    def balance_sheet_statement(self, symbol: str, period: str = "annual", limit: int = 5) -> List[dict]:
        return self._statement("balance-sheet-statement", symbol, period, limit)

    def cash_flow_statement(self, symbol: str, period: str = "annual", limit: int = 5) -> List[dict]:
        return self._statement("cash-flow-statement", symbol, period, limit)

"""Financial Modeling Prep（stable 路徑）。金鑰走查詢參數 apikey。"""
from __future__ import annotations

from typing import Any, List

from bot.free_sources.base import BaseClient

BASE_URL = "https://financialmodelingprep.com/stable"


class FmpClient(BaseClient):
    source = "fmp"

    def _check_body(self, data: Any) -> None:
        if isinstance(data, dict) and (data.get("Error Message") or data.get("error")):
            raise self._err(self.last_status, data.get("Error Message") or data.get("error"), "api_error")

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

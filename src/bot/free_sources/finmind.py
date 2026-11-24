"""FinMind：台股資料集。金鑰走 Authorization: Bearer。"""
from __future__ import annotations

from typing import Any, List

from bot.free_sources.base import BaseClient

DATA_URL = "https://api.finmindtrade.com/api/v4/data"
USER_INFO_URL = "https://api.web.finmindtrade.com/v2/user_info"


class FinMindClient(BaseClient):
    source = "finmind"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _check_body(self, data: Any) -> None:
        # FinMind 會在 body 內放 status（例如 402 額度用完）
        if isinstance(data, dict):
            status = data.get("status")
            if isinstance(status, int) and status >= 400:
                self.last_status = status
                raise self._err(status, data.get("msg") or "error")

    def data(self, dataset: str, data_id: str, start_date: str, end_date: str) -> List[dict]:
        params = {
            "dataset": dataset,
            "data_id": data_id,
            "start_date": str(start_date),
            "end_date": str(end_date),
        }
        body = self._get(DATA_URL, params, self._headers())
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise self._err(self.last_status, "missing data array", "bad_response")
        return rows

    def user_info(self) -> dict:
        return self._get(USER_INFO_URL, None, self._headers())

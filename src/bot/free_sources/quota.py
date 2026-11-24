"""每日 / 每小時 / 每分鐘用量計數器。

計數存成 JSON（每個來源一個檔），日期以台灣時間為 key；超過上限就 raise
QuotaExceeded，呼叫端不得送出請求。
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

TAIPEI = dt.timezone(dt.timedelta(hours=8))
DEFAULT_QUOTA_DIR = Path("data/api_quota")

# 依文件的免費額度留餘裕。None = 不限制該維度。
DEFAULT_LIMITS: Dict[str, Dict[str, Optional[int]]] = {
    "finnhub": {"daily": 1500, "per_minute": 50, "per_hour": None},
    "fugle": {"daily": 3000, "per_minute": 50, "per_hour": None},
    "finmind": {"daily": None, "per_minute": None, "per_hour": 500},
    "fmp": {"daily": 230, "per_minute": None, "per_hour": None},
    "massive": {"daily": 200, "per_minute": 4, "per_hour": None},
    "marketaux": {"daily": 90, "per_minute": None, "per_hour": None},
    "alphavantage": {"daily": 20, "per_minute": None, "per_hour": None},
}


class QuotaExceeded(Exception):
    def __init__(self, source: str, window: str, limit: int) -> None:
        self.source = source
        self.window = window
        self.limit = limit
        super().__init__(f"{source}: {window} quota exceeded (limit {limit})")


def _now_taipei() -> dt.datetime:
    return dt.datetime.now(TAIPEI)


class QuotaTracker:
    def __init__(
        self,
        source: str,
        *,
        daily_limit: Optional[int] = None,
        per_minute: Optional[int] = None,
        per_hour: Optional[int] = None,
        directory: Optional[Path] = None,
        now: Optional[Callable[[], dt.datetime]] = None,
    ) -> None:
        self.source = source
        self.daily_limit = daily_limit
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.directory = Path(directory) if directory is not None else DEFAULT_QUOTA_DIR
        self._now = now or _now_taipei
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.directory / f"{self.source}.json"

    def _load(self, today: str) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("date") != today:
            data = {"date": today, "count": 0, "stamps": []}
        data.setdefault("count", 0)
        data.setdefault("stamps", [])
        return data

    def used_today(self) -> int:
        now = self._now().astimezone(TAIPEI)
        return int(self._load(now.date().isoformat())["count"])

    def consume(self) -> None:
        """登記一次請求；任何一個維度超限就 raise，且不計數。"""
        with self._lock:
            now = self._now().astimezone(TAIPEI)
            ts = now.timestamp()
            data = self._load(now.date().isoformat())
            stamps = [s for s in data["stamps"] if isinstance(s, (int, float)) and ts - s < 3600]
            if self.daily_limit is not None and data["count"] >= self.daily_limit:
                raise QuotaExceeded(self.source, "daily", self.daily_limit)
            if self.per_hour is not None and len(stamps) >= self.per_hour:
                raise QuotaExceeded(self.source, "hourly", self.per_hour)
            if self.per_minute is not None:
                if sum(1 for s in stamps if ts - s < 60) >= self.per_minute:
                    raise QuotaExceeded(self.source, "per-minute", self.per_minute)
            stamps.append(ts)
            data["count"] += 1
            data["stamps"] = stamps
            self.directory.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), encoding="utf-8")


def default_quota(
    source: str,
    directory: Optional[Path] = None,
    now: Optional[Callable[[], dt.datetime]] = None,
) -> QuotaTracker:
    limits = DEFAULT_LIMITS[source]
    return QuotaTracker(
        source,
        daily_limit=limits["daily"],
        per_minute=limits["per_minute"],
        per_hour=limits["per_hour"],
        directory=directory,
        now=now,
    )

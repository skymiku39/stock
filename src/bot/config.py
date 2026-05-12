from __future__ import annotations

import datetime
from typing import List, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 執行模式 ---
    run_mode: Literal["trade", "watch", "report"] = "trade"
    market_source: Literal["shioaji", "twse_public", ""] = ""
    report_poll_seconds: int = 5
    report_output_dir: str = "data/reports"

    # --- Shioaji 登入 ---
    api_key: str = ""
    secret_key: str = ""

    # --- 電子憑證 ---
    ca_path: str = ""
    ca_password: str = ""
    person_id: str = ""

    # --- 模擬模式 ---
    simulation: bool = True

    # --- 監控股票 (逗號分隔, e.g. "2330,0050,2881") ---
    symbols: List[str] = []

    @field_validator("symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, v: object) -> List[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return list(v)  # type: ignore[arg-type]

    # --- 策略時間 ---
    enter_cutoff_time: datetime.time = datetime.time(9, 30)
    exit_time: datetime.time = datetime.time(13, 15)

    @field_validator("enter_cutoff_time", "exit_time", mode="before")
    @classmethod
    def _parse_time(cls, v: object) -> datetime.time:
        if isinstance(v, datetime.time):
            return v
        if isinstance(v, str):
            parts = v.split(":")
            return datetime.time(int(parts[0]), int(parts[1]))
        raise ValueError(f"Cannot parse time from {v!r}")

    # --- 停損停利 (百分比) ---
    stop_loss_pct: float = -3.0
    take_profit_pct: float = 6.0
    trailing_stop_pct: float = 2.0  # 從最高點回撤此百分比則觸發停利

    # --- 資金控管 ---
    max_fund: int = 500_000
    max_lot_per_symbol: int = 2

    # --- Telegram 通知 (留空則不啟用) ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @model_validator(mode="after")
    def _resolve_market_source(self) -> Settings:
        if not self.market_source:
            self.market_source = (  # type: ignore[assignment]
                "twse_public" if self.run_mode == "report" else "shioaji"
            )
        return self

    @model_validator(mode="after")
    def _validate_mode_requirements(self) -> Settings:
        if self.run_mode in ("trade", "watch") and self.market_source == "twse_public":
            raise ValueError(
                f"run_mode={self.run_mode!r} 需要 Shioaji 行情，"
                "market_source 不可為 'twse_public'"
            )
        if self.run_mode == "trade" and not self.api_key:
            pass  # api_key 可由 .env 延後提供，不在此強制檢查
        return self

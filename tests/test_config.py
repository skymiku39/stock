"""Settings 解析測試。"""

from __future__ import annotations

import pytest

from bot.config import Settings


class TestRunModeDefaults:
    def test_default_is_trade(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RUN_MODE", raising=False)
        s = Settings(symbols=["2330"], _env_file=None)  # type: ignore[call-arg]
        assert s.run_mode == "trade"
        assert s.market_source == "shioaji"

    def test_report_auto_market_source(self) -> None:
        s = Settings(run_mode="report", symbols=["2330"], _env_file=None)  # type: ignore[call-arg]
        assert s.market_source == "twse_public"

    def test_watch_auto_market_source(self) -> None:
        s = Settings(run_mode="watch", symbols=["2330"], _env_file=None)  # type: ignore[call-arg]
        assert s.market_source == "shioaji"

    def test_explicit_market_source_preserved(self) -> None:
        s = Settings(
            run_mode="report", market_source="twse_public",
            symbols=["2330"], _env_file=None,  # type: ignore[call-arg]
        )
        assert s.market_source == "twse_public"

    def test_trade_with_twse_public_raises(self) -> None:
        with pytest.raises(Exception):
            Settings(
                run_mode="trade", market_source="twse_public",
                symbols=["2330"], _env_file=None,  # type: ignore[call-arg]
            )

    def test_report_with_shioaji_raises(self) -> None:
        with pytest.raises(Exception):
            Settings(
                run_mode="report", market_source="shioaji",
                symbols=["2330"], _env_file=None,  # type: ignore[call-arg]
            )


class TestReportSettings:
    def test_poll_seconds_default(self) -> None:
        s = Settings(run_mode="report", symbols=["2330"], _env_file=None)  # type: ignore[call-arg]
        assert s.report_poll_seconds == 5

    def test_poll_seconds_custom(self) -> None:
        s = Settings(
            run_mode="report", report_poll_seconds=10,
            symbols=["2330"], _env_file=None,  # type: ignore[call-arg]
        )
        assert s.report_poll_seconds == 10

    def test_poll_seconds_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            Settings(
                run_mode="report", report_poll_seconds=0,
                symbols=["2330"], _env_file=None,  # type: ignore[call-arg]
            )

    def test_output_dir_default(self) -> None:
        s = Settings(run_mode="report", symbols=["2330"], _env_file=None)  # type: ignore[call-arg]
        assert s.report_output_dir == "data/reports"


class TestSymbolParsing:
    def test_comma_separated(self) -> None:
        s = Settings(symbols="2330,0050,2881", _env_file=None)  # type: ignore[call-arg]
        assert s.symbols == ["2330", "0050", "2881"]

    def test_list_input(self) -> None:
        s = Settings(symbols=["2330"], _env_file=None)  # type: ignore[call-arg]
        assert s.symbols == ["2330"]

    def test_dotenv_comma_separated(self, tmp_path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "RUN_MODE=watch\n"
            "API_KEY=test\n"
            "SECRET_KEY=test\n"
            "SYMBOLS=2330,0050,2881\n",
            encoding="utf-8",
        )

        s = Settings(_env_file=env_file)  # type: ignore[call-arg]

        assert s.symbols == ["2330", "0050", "2881"]

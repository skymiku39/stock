"""Settings 解析測試。"""

from __future__ import annotations

import pytest

from bot.config import Settings


class TestRunModeDefaults:
    def test_default_is_watch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RUN_MODE", raising=False)
        s = Settings(symbols=["2330"], _env_file=None)  # type: ignore[call-arg]
        assert s.run_mode == "watch"
        assert s.market_source == "shioaji"
        assert s.day_trading_archived is True

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


class TestSellProfitTargets:
    def test_sell_profit_targets_parse_comma_pairs(self) -> None:
        s = Settings(
            symbols=["2330"],
            sell_profit_targets="2330:8,0050=5.5",
            _env_file=None,  # type: ignore[call-arg]
        )

        assert s.sell_profit_targets == {"2330": 8.0, "0050": 5.5}

    def test_sell_profit_targets_default_empty(self) -> None:
        s = Settings(symbols=["2330"], _env_file=None)  # type: ignore[call-arg]
        assert s.sell_profit_targets == {}


class TestBuyEntryTargets:
    def test_parse_triples(self) -> None:
        s = Settings(
            symbols=["2330"],
            buy_entry_targets="2330:1:5,0050:0.5:3",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert s.buy_entry_targets == {"2330": (1.0, 5.0), "0050": (0.5, 3.0)}


class TestSimulationSettings:
    def test_defaults(self) -> None:
        s = Settings(symbols=["2330"], _env_file=None)  # type: ignore[call-arg]
        assert s.min_pct_chg_on_entry == 1.0
        assert s.use_odd_lot is False
        assert s.llm_gate_enabled is False
        assert s.strategy_type == "configurable"

    def test_strategy_type_default_alias(self) -> None:
        s = Settings(
            symbols=["2330"],
            strategy_type="default",
            _env_file=None,  # type: ignore[call-arg]
        )
        assert s.strategy_type == "configurable"

    def test_configurable_strategy_type(self) -> None:
        s = Settings(
            symbols=["2330"],
            strategy_type="configurable",
            use_odd_lot=True,
            llm_gate_enabled=True,
            _env_file=None,  # type: ignore[call-arg]
        )
        assert s.strategy_type == "configurable"
        assert s.use_odd_lot is True
        assert s.llm_gate_enabled is True

"""fundamentals_fetcher / quarterly / chip_distribution / technicals 模組單元測試。

測試重點放在「資料解析 + 衍生指標」，不發 HTTP，避免測試依賴外部網路。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pandas as pd
import pytest

from bot.chip_distribution import (
    DistributionTrend,
    DistributionWeekly,
    interpret_distribution,
    parse_distribution_for_ticker,
)
from bot.fundamentals_fetcher import (
    DividendRecord,
    FundamentalSnapshot,
    MonthlyRevenue,
    QuarterlyFinancials,
    ValuationDaily,
    _enrich_dividends_with_payout,
    _parse_one_revenue,
    _parse_valuation,
    load_manual_quarterlies,
    save_manual_quarterlies,
)
from bot.quarterly import (
    revenue_quarterly_aggregate,
    rolling_eps_series,
    summarize_quarterly,
)
from bot.technicals import (
    _enumerate_months,
    add_kd,
    add_macd,
    add_moving_averages,
    add_rsi,
    compute_indicators,
    derive_signals,
    extend_kline_backward,
    fetch_kline_range,
    get_kline_coverage,
)


# ----------------------------------------------------------------------
# fundamentals
# ----------------------------------------------------------------------


class TestParseRevenue:
    def test_parses_full_fields(self) -> None:
        raw = {
            "公司代號": "2330",
            "公司名稱": "台積電",
            "資料年月": "11410",
            "營業收入-當月營收": "300,000",
            "營業收入-去年當月營收": "260,000",
            "營業收入-去年同月增減(%)": "15.4",
            "營業收入-上月比較增減(%)": "-2.1",
            "營業收入-當月累計營收": "2,400,000",
            "營業收入-前期比較增減(%)": "30.5",
        }
        mr = _parse_one_revenue(raw)
        assert mr is not None
        assert mr.ticker == "2330"
        assert mr.year == 2025
        assert mr.month == 10
        assert mr.revenue == pytest.approx(300000)
        assert mr.yoy == pytest.approx(15.4)
        assert mr.mom == pytest.approx(-2.1)
        assert mr.cum_revenue == pytest.approx(2400000)

    def test_missing_code_returns_none(self) -> None:
        assert _parse_one_revenue({"資料年月": "11401"}) is None


class TestParseValuation:
    def test_parses_with_pe_pb_dy(self) -> None:
        raw = {
            "Code": "2330",
            "Name": "台積電",
            "PEratio": "22.5",
            "PBratio": "5.3",
            "DividendYield": "1.85",
            "FinancialYear": "2024",
        }
        v = _parse_valuation(raw)
        assert v is not None
        assert v.ticker == "2330"
        assert v.pe_ratio == pytest.approx(22.5)
        assert v.dividend_yield == pytest.approx(1.85)


class TestPayoutEnrichment:
    def test_calculates_payout_ratio(self) -> None:
        divs = [DividendRecord(ticker="2330", year=2024, cash_dividend=10, stock_dividend=0)]
        qs = [
            QuarterlyFinancials("2330", year=2024, quarter=q, eps=4.0)
            for q in (1, 2, 3, 4)
        ]
        out = _enrich_dividends_with_payout(divs, qs)
        # 10 / 16 = 62.5%
        assert out[0].payout_ratio == pytest.approx(62.5)

    def test_no_eps_keeps_payout_none(self) -> None:
        divs = [DividendRecord(ticker="2330", year=2024, cash_dividend=5)]
        out = _enrich_dividends_with_payout(divs, [])
        assert out[0].payout_ratio is None


class TestFundamentalSnapshotDerived:
    def test_yoy_streak_counts_from_latest(self) -> None:
        snap = FundamentalSnapshot(ticker="2330")
        snap.revenues = [
            MonthlyRevenue("2330", year=2024, month=10, yoy=10.0),
            MonthlyRevenue("2330", year=2024, month=11, yoy=12.0),
            MonthlyRevenue("2330", year=2024, month=12, yoy=15.0),
            MonthlyRevenue("2330", year=2025, month=1, yoy=-2.0),  # 中斷
            MonthlyRevenue("2330", year=2025, month=2, yoy=8.0),
            MonthlyRevenue("2330", year=2025, month=3, yoy=20.0),
        ]
        # 最新月為 2025/3 → 連續 2 個月正成長 (2025/2, 2025/3)
        assert snap.revenue_yoy_streak() == 2

    def test_rolling_eps_progress(self) -> None:
        snap = FundamentalSnapshot(ticker="2330")
        snap.quarterlies = [
            QuarterlyFinancials("2330", year=2024, quarter=q, eps=4.0)
            for q in (1, 2, 3, 4)
        ] + [
            QuarterlyFinancials("2330", year=2025, quarter=q, eps=5.0)
            for q in (1, 2)
        ]
        prog = snap.rolling_eps_progress()
        assert prog["year"] == 2025
        assert len(prog["progress"]) == 2
        assert prog["progress"][0]["cum_eps"] == pytest.approx(5.0)
        assert prog["prev_year_total_eps"] == pytest.approx(16.0)


class TestManualQuarterlyIO:
    def test_save_and_load(self, tmp_path: Path) -> None:
        qs = [
            QuarterlyFinancials("2330", year=2025, quarter=3, eps=14.71, gross_margin=59.1),
        ]
        save_manual_quarterlies("2330", qs, root=tmp_path)
        loaded = load_manual_quarterlies("2330", root=tmp_path)
        assert len(loaded) == 1
        assert loaded[0].eps == pytest.approx(14.71)
        assert loaded[0].gross_margin == pytest.approx(59.1)


# ----------------------------------------------------------------------
# quarterly
# ----------------------------------------------------------------------


class TestQuarterlyAggregation:
    def test_revenue_quarterly_aggregate(self) -> None:
        revs = [
            MonthlyRevenue("2330", year=2025, month=m, revenue=100, revenue_last_year=80)
            for m in range(1, 7)
        ]
        out = revenue_quarterly_aggregate(revs)
        assert len(out) == 2
        q1, q2 = out
        assert q1.year == 2025 and q1.quarter == 1
        assert q1.revenue == 300
        assert q1.yoy == pytest.approx(25.0)
        assert q2.quarter == 2

    def test_rolling_eps_diff_pct_and_note(self) -> None:
        qs = [
            QuarterlyFinancials("2330", year=2024, quarter=q, eps=4.0)
            for q in (1, 2, 3, 4)
        ] + [
            QuarterlyFinancials("2330", year=2025, quarter=q, eps=5.0)
            for q in (1, 2, 3)
        ]
        points = rolling_eps_series(qs)
        nine_m = [p for p in points if p.year == 2025 and p.label == "9M"][0]
        # 2025 9M EPS = 15.0 / 2024 9M EPS = 12 → +25%
        assert nine_m.eps_sum == pytest.approx(15.0)
        assert nine_m.diff_pct == pytest.approx(25.0)
        # 2024 全年 = 16，9M = 15 < 16，所以 note 不應該觸發
        assert "超越" not in nine_m.note

    def test_summarize_quarterly_returns_focus(self) -> None:
        out = summarize_quarterly([], [], today=dt.date(2025, 5, 1))
        assert "current_focus" in out
        assert out["current_focus"]["quarter"] == 4
        assert out["current_focus"]["year"] == 2024


# ----------------------------------------------------------------------
# chip_distribution
# ----------------------------------------------------------------------


SAMPLE_TDCC_ROWS = [
    {
        "資料日期": "20250523",
        "證券代號": "2330",
        "持股分級": 1,
        "人數": 100000,
        "股數": 50000000,
        "占集保庫存數比例(%)": 0.5,
    },
    {
        "資料日期": "20250523",
        "證券代號": "2330",
        "持股分級": 2,
        "人數": 80000,
        "股數": 200000000,
        "占集保庫存數比例(%)": 2.0,
    },
    {
        "資料日期": "20250523",
        "證券代號": "2330",
        "持股分級": 14,
        "人數": 50,
        "股數": 1000000000,
        "占集保庫存數比例(%)": 10.0,
    },
    {
        "資料日期": "20250523",
        "證券代號": "2330",
        "持股分級": 15,
        "人數": 100,
        "股數": 7000000000,
        "占集保庫存數比例(%)": 70.0,
    },
]


class TestDistributionParsing:
    def test_parse_distribution_calculates_buckets(self) -> None:
        snap = parse_distribution_for_ticker(SAMPLE_TDCC_ROWS, "2330")
        assert snap is not None
        assert snap.ticker == "2330"
        assert snap.week_date == "2025-05-23"
        # large = level 12-15; 樣本只有 14, 15
        assert snap.large_holder_pct == pytest.approx(80.0)
        assert snap.whale_holder_pct == pytest.approx(80.0)
        # retail = level 1-3; 樣本有 1, 2
        assert snap.retail_holder_pct == pytest.approx(2.5)
        assert snap.total_holders == 180150

    def test_parse_unknown_ticker_returns_none(self) -> None:
        snap = parse_distribution_for_ticker(SAMPLE_TDCC_ROWS, "9999")
        assert snap is None


class TestDistributionInterpretation:
    def test_accumulation_when_large_up_retail_down(self) -> None:
        trend = DistributionTrend(
            ticker="2330",
            weeks=[
                DistributionWeekly("2330", week_date="2025-04-01",
                                   large_holder_pct=70, retail_holder_pct=5),
                DistributionWeekly("2330", week_date="2025-04-08",
                                   large_holder_pct=70.5, retail_holder_pct=4.9),
                DistributionWeekly("2330", week_date="2025-04-15",
                                   large_holder_pct=71.0, retail_holder_pct=4.7),
                DistributionWeekly("2330", week_date="2025-04-22",
                                   large_holder_pct=71.8, retail_holder_pct=4.5),
            ],
        )
        label, _, score = interpret_distribution(trend)
        assert label == "accumulation"
        assert score >= 50

    def test_distribution_when_large_down_retail_up(self) -> None:
        trend = DistributionTrend(
            ticker="2330",
            weeks=[
                DistributionWeekly("2330", week_date="2025-04-01",
                                   large_holder_pct=70, retail_holder_pct=5),
                DistributionWeekly("2330", week_date="2025-04-22",
                                   large_holder_pct=68.5, retail_holder_pct=5.5),
            ],
        )
        label, _, score = interpret_distribution(trend)
        assert label == "distribution"
        assert score <= 55


# ----------------------------------------------------------------------
# technicals
# ----------------------------------------------------------------------


def _sample_df() -> pd.DataFrame:
    # 40 個交易日的合成資料，週期性上漲
    rows = []
    base = 100.0
    for i in range(40):
        close = base + i * 0.5 + (i % 4) - 2
        rows.append({
            "date": (dt.date(2025, 1, 1) + dt.timedelta(days=i)).isoformat(),
            "open": close - 0.3,
            "high": close + 0.8,
            "low": close - 1.0,
            "close": close,
            "volume": 1000 + i * 10,
        })
    return pd.DataFrame(rows)


class TestTechnicalIndicators:
    def test_moving_averages_fill_lengths(self) -> None:
        df = add_moving_averages(_sample_df(), [5, 10])
        assert "ma5" in df.columns
        assert df["ma5"].iloc[-1] > df["ma5"].iloc[0]

    def test_rsi_in_range(self) -> None:
        df = add_rsi(_sample_df(), period=14)
        last = float(df["rsi14"].iloc[-1])
        assert 0 <= last <= 100

    def test_macd_signal_and_hist(self) -> None:
        df = add_macd(_sample_df())
        assert "macd" in df.columns
        assert "macd_signal" in df.columns
        assert "macd_hist" in df.columns

    def test_kd_in_range(self) -> None:
        df = add_kd(_sample_df())
        last_k = float(df["k"].iloc[-1])
        last_d = float(df["d"].iloc[-1])
        assert 0 <= last_k <= 100
        assert 0 <= last_d <= 100

    def test_compute_indicators_returns_full(self) -> None:
        df = compute_indicators(_sample_df())
        for col in ("ma5", "ma20", "ma60", "rsi14", "macd", "k", "d", "boll_mid"):
            assert col in df.columns

    def test_derive_signals_emits_basic_ones(self) -> None:
        df = compute_indicators(_sample_df())
        sigs = derive_signals(df)
        labels = {s.label for s in sigs}
        assert "MACD" in labels
        assert "月線位置" in labels


# ----------------------------------------------------------------------
# K 線批次抓取 (fetch_kline_range / extend_kline_backward / coverage)
# ----------------------------------------------------------------------


def _fake_month_rows(ticker: str, year: int, month: int) -> List[Dict[str, Any]]:
    """虛擬 K 線：每月 5 根 K 棒。"""
    out = []
    for d in (3, 7, 14, 21, 28):
        try:
            iso = dt.date(year, month, d).isoformat()
        except ValueError:
            continue
        out.append({
            "date": iso,
            "open": 100.0 + d,
            "high": 102.0 + d,
            "low": 99.0 + d,
            "close": 101.0 + d,
            "volume": 1000.0 + d,
        })
    return out


class TestEnumerateMonths:
    def test_includes_endpoints(self) -> None:
        months = _enumerate_months(dt.date(2024, 11, 5), dt.date(2025, 2, 10))
        assert months == [(2024, 11), (2024, 12), (2025, 1), (2025, 2)]

    def test_same_month(self) -> None:
        months = _enumerate_months(dt.date(2025, 3, 1), dt.date(2025, 3, 28))
        assert months == [(2025, 3)]

    def test_year_boundary(self) -> None:
        months = _enumerate_months(dt.date(2024, 12, 1), dt.date(2025, 1, 1))
        assert months == [(2024, 12), (2025, 1)]


class TestFetchKlineRange:
    def test_writes_csv_and_progress_called(self, tmp_path: Path) -> None:
        captured = []

        def fake_fetch(ticker, y, m, market=None, session=None, logger=None):
            return _fake_month_rows(ticker, y, m)

        def on_progress(idx, total, label, fetched):
            captured.append((idx, total, label, fetched))

        with patch("bot.technicals.fetch_monthly_kline", side_effect=fake_fetch):
            df = fetch_kline_range(
                "2330",
                start_date=dt.date(2024, 11, 1),
                end_date=dt.date(2025, 1, 31),
                root=tmp_path,
                save_to_db=False,
                request_delay_sec=0,
                on_progress=on_progress,
            )

        assert not df.empty
        # 3 個月 × 5 根 = 15 (除非月底超出，但本例 11~1 月都有 28 號)
        assert len(df) == 15
        # progress 應該被呼叫 3 次
        assert len(captured) == 3
        # CSV 寫入
        csv_path = tmp_path / "data" / "technicals" / "2330" / "daily_kline.csv"
        assert csv_path.exists()
        loaded = pd.read_csv(csv_path)
        assert len(loaded) == 15

    def test_skip_existing_months(self, tmp_path: Path) -> None:
        # 先寫一份 cache：2024/12 已有資料
        csv_dir = tmp_path / "data" / "technicals" / "2330"
        csv_dir.mkdir(parents=True)
        pd.DataFrame(_fake_month_rows("2330", 2024, 12)).to_csv(
            csv_dir / "daily_kline.csv", index=False,
        )

        calls = []

        def fake_fetch(ticker, y, m, market=None, session=None, logger=None):
            calls.append((y, m))
            return _fake_month_rows(ticker, y, m)

        with patch("bot.technicals.fetch_monthly_kline", side_effect=fake_fetch):
            fetch_kline_range(
                "2330",
                start_date=dt.date(2024, 11, 1),
                end_date=dt.date(2025, 1, 31),
                root=tmp_path,
                save_to_db=False,
                skip_existing_months=True,
                request_delay_sec=0,
            )

        # 2024/12 是 cache 的邊界 (本例 cache 只含 12) → 邊界月會被重抓
        # 11 月與 1 月一定要抓
        assert (2024, 11) in calls
        assert (2025, 1) in calls

    def test_direction_backward_iterates_newest_first(self, tmp_path: Path) -> None:
        order = []

        def fake_fetch(ticker, y, m, market=None, session=None, logger=None):
            order.append((y, m))
            return _fake_month_rows(ticker, y, m)

        with patch("bot.technicals.fetch_monthly_kline", side_effect=fake_fetch):
            fetch_kline_range(
                "2330",
                start_date=dt.date(2024, 11, 1),
                end_date=dt.date(2025, 1, 31),
                root=tmp_path,
                save_to_db=False,
                request_delay_sec=0,
                direction="backward",
            )

        assert order == [(2025, 1), (2024, 12), (2024, 11)]

    def test_empty_when_start_after_end(self, tmp_path: Path) -> None:
        with patch("bot.technicals.fetch_monthly_kline", return_value=[]):
            df = fetch_kline_range(
                "2330",
                start_date=dt.date(2025, 3, 1),
                end_date=dt.date(2025, 1, 1),
                root=tmp_path,
                save_to_db=False,
            )
        assert df.empty


class TestGetKlineCoverage:
    def test_returns_none_when_no_cache(self, tmp_path: Path) -> None:
        assert get_kline_coverage("9999", root=tmp_path) is None

    def test_reads_csv_cache(self, tmp_path: Path) -> None:
        csv_dir = tmp_path / "data" / "technicals" / "2330"
        csv_dir.mkdir(parents=True)
        rows = _fake_month_rows("2330", 2020, 1) + _fake_month_rows("2330", 2025, 5)
        pd.DataFrame(rows).to_csv(csv_dir / "daily_kline.csv", index=False)
        cov = get_kline_coverage("2330", root=tmp_path)
        assert cov is not None
        assert cov["earliest"] == "2020-01-03"
        assert cov["latest"] == "2025-05-28"
        assert cov["rows"] == 10


class TestExtendBackward:
    def test_uses_anchor_from_cache(self, tmp_path: Path) -> None:
        csv_dir = tmp_path / "data" / "technicals" / "2330"
        csv_dir.mkdir(parents=True)
        # cache 最早是 2024/01
        pd.DataFrame(_fake_month_rows("2330", 2024, 1)).to_csv(
            csv_dir / "daily_kline.csv", index=False,
        )

        called_months = []

        def fake_fetch(ticker, y, m, market=None, session=None, logger=None):
            called_months.append((y, m))
            return _fake_month_rows(ticker, y, m)

        with patch("bot.technicals.fetch_monthly_kline", side_effect=fake_fetch):
            extend_kline_backward(
                "2330", years_back=2, root=tmp_path,
                save_to_db=False, request_delay_sec=0,
            )

        # 應該呼叫 2022/01 ~ 2023/12 之間月份
        assert any(y == 2022 for y, _ in called_months)
        assert any(y == 2023 for y, _ in called_months)
        # 不應該觸到 2024 (anchor 之後)
        assert all(y < 2024 for y, _ in called_months)

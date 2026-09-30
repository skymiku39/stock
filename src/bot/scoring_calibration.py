"""評分系統的歷史校準與最小可用回測介面。

這不是保證獲利的策略回測器，而是把「某日產生的訊號」與之後第 N 個
交易日的收盤報酬配對，檢查分數／動作是否有方向性。資料不足時會明確回傳
``insufficient``，不把少量樣本包裝成可靠結論。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


BUY_ACTIONS = {"BUY", "STRONG_BUY", "LONG", "ADD", "加碼", "買進"}
SELL_ACTIONS = {"SELL", "REDUCE", "SHORT", "減碼", "賣出"}
HOLD_ACTIONS = {"HOLD", "WAIT", "NEUTRAL", "觀望", "持有"}


@dataclass
class CalibrationSample:
    ticker: str
    signal_date: str
    score: float
    action: str
    horizon_days: int
    entry_close: float
    exit_date: str
    exit_close: float
    forward_return_pct: float
    label: str  # positive | negative | neutral


@dataclass
class SampleBuildResult:
    samples: List[CalibrationSample] = field(default_factory=list)
    skipped: int = 0
    skip_reasons: Dict[str, int] = field(default_factory=dict)

    def add_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


@dataclass
class CalibrationReport:
    horizon_days: int
    sample_count: int
    skipped_count: int
    status: str  # insufficient | exploratory
    mean_forward_return_pct: Optional[float] = None
    directional_sample_count: int = 0
    directional_accuracy: Optional[float] = None
    action_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    label_counts: Dict[str, int] = field(default_factory=dict)
    score_buckets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    skip_reasons: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value: Any) -> str:
    text = _text(value).replace("/", "-")
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return ""


def _first(row: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if _text(row.get(name)):
            return row.get(name)
    return None


def _ticker(row: Mapping[str, Any]) -> str:
    return _text(_first(row, ("ticker", "symbol", "Code", "code")))


def _signal_date(row: Mapping[str, Any]) -> str:
    return _date(_first(row, ("signal_date", "date", "ts", "asof", "updated_at")))


def _score(row: Mapping[str, Any]) -> Optional[float]:
    direct = _first(row, ("score", "total_score", "overall_score", "day_trade_score"))
    if direct is not None:
        value = _number(direct)
        if value is not None:
            return max(0.0, min(100.0, value))
    sentiment = _number(row.get("sentiment_score"))
    if sentiment is None:
        return None
    confidence = _number(row.get("confidence"))
    confidence = 1.0 if confidence is None else max(0.0, min(1.0, confidence))
    # llm_analysis_history 的 sentiment_score 是 -1..1；低信心訊號向中性收縮。
    return max(0.0, min(100.0, 50.0 + sentiment * 50.0 * confidence))


def action_from_score(score: float) -> str:
    if score >= 70.0:
        return "BUY"
    if score <= 30.0:
        return "SELL"
    return "HOLD"


def _action(row: Mapping[str, Any], score: float) -> str:
    raw = _text(_first(row, ("action", "decision", "recommendation"))).upper()
    if raw in BUY_ACTIONS:
        return "BUY"
    if raw in SELL_ACTIONS:
        return "SELL"
    if raw in HOLD_ACTIONS:
        return "HOLD"
    return action_from_score(score)


def _signal_quality_skip_reason(row: Mapping[str, Any]) -> str:
    """回傳不可用訊號的原因；空字串代表舊格式仍可解析。"""
    success = row.get("success")
    if success is not None and str(success).strip().lower() in {
        "0", "false", "no", "failed", "error",
    }:
        return "llm_failed"
    quality = _text(row.get("quality_status")).lower()
    if quality and quality not in {"ok", "partial"}:
        return "low_quality"
    return ""


def _label(forward_return_pct: float, positive_threshold: float, negative_threshold: float) -> str:
    if forward_return_pct >= positive_threshold:
        return "positive"
    if forward_return_pct <= negative_threshold:
        return "negative"
    return "neutral"


def build_calibration_samples(
    signal_rows: Iterable[Mapping[str, Any]],
    price_rows: Iterable[Mapping[str, Any]],
    *,
    horizon_days: int = 5,
    positive_threshold: float = 2.0,
    negative_threshold: float = -2.0,
) -> SampleBuildResult:
    """將歷史訊號與未來第 N 根日 K 收盤配對。

    僅使用嚴格晚於訊號日的價格，避免把當日收盤或未來資料洩漏回訊號。
    """
    result = SampleBuildResult()
    if horizon_days <= 0:
        raise ValueError("horizon_days 必須大於 0")

    prices: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for row in price_rows:
        ticker = _ticker(row)
        date = _date(row.get("date"))
        close = _number(row.get("close"))
        if not ticker or not date or close is None or close <= 0:
            continue
        prices[ticker].append((date, close))
    for ticker in prices:
        prices[ticker].sort(key=lambda item: item[0])

    for signal in signal_rows:
        quality_skip = _signal_quality_skip_reason(signal)
        if quality_skip:
            result.add_skip(quality_skip)
            continue
        ticker = _ticker(signal)
        signal_date = _signal_date(signal)
        score = _score(signal)
        if not ticker:
            result.add_skip("missing_ticker")
            continue
        if not signal_date:
            result.add_skip("invalid_signal_date")
            continue
        if score is None:
            result.add_skip("missing_score")
            continue
        history = prices.get(ticker, [])
        future = [(date, close) for date, close in history if date > signal_date]
        if len(future) < horizon_days:
            result.add_skip("insufficient_forward_prices")
            continue
        entry_candidates = [close for date, close in history if date == signal_date]
        if not entry_candidates:
            result.add_skip("missing_entry_price")
            continue
        entry_close = entry_candidates[-1]
        exit_date, exit_close = future[horizon_days - 1]
        forward = (exit_close / entry_close - 1.0) * 100.0
        result.samples.append(CalibrationSample(
            ticker=ticker,
            signal_date=signal_date,
            score=score,
            action=_action(signal, score),
            horizon_days=horizon_days,
            entry_close=entry_close,
            exit_date=exit_date,
            exit_close=exit_close,
            forward_return_pct=forward,
            label=_label(forward, positive_threshold, negative_threshold),
        ))
    return result


def _score_bucket(score: float) -> str:
    lower = min(80, int(score // 20) * 20)
    upper = lower + 19 if lower < 80 else 100
    return f"{lower}-{upper}"


def evaluate_calibration(
    build: SampleBuildResult,
    *,
    horizon_days: int,
    minimum_samples: int = 30,
) -> CalibrationReport:
    """產生探索性校準報告；樣本不足時 status 保持 ``insufficient``。"""
    samples = build.samples
    returns = [sample.forward_return_pct for sample in samples]
    label_counts = Counter(sample.label for sample in samples)
    directional = [
        sample for sample in samples
        if sample.label != "neutral" and sample.action != "HOLD"
    ]
    correct = sum(
        1 for sample in directional
        if (sample.action == "BUY" and sample.label == "positive")
        or (sample.action == "SELL" and sample.label == "negative")
    )
    action_groups: Dict[str, List[CalibrationSample]] = defaultdict(list)
    for sample in samples:
        action_groups[sample.action].append(sample)
    action_stats = {
        action: {
            "count": len(group),
            "mean_forward_return_pct": sum(s.forward_return_pct for s in group) / len(group),
            "positive_rate": sum(s.label == "positive" for s in group) / len(group),
        }
        for action, group in sorted(action_groups.items())
    }
    score_groups: Dict[str, List[CalibrationSample]] = defaultdict(list)
    for sample in samples:
        score_groups[_score_bucket(sample.score)].append(sample)
    score_buckets = {
        bucket: {
            "count": len(group),
            "mean_forward_return_pct": sum(s.forward_return_pct for s in group) / len(group),
            "positive_rate": sum(s.label == "positive" for s in group) / len(group),
        }
        for bucket, group in sorted(score_groups.items())
    }
    status = "exploratory" if len(samples) >= minimum_samples else "insufficient"
    notes = [
        "結果只反映歷史配對，不代表未來報酬或可直接下單。",
        f"至少需要 {minimum_samples} 個有效樣本才會標示 exploratory。",
    ]
    if not samples:
        notes.append("目前沒有可配對的訊號與未來價格，不能校準權重。")
    return CalibrationReport(
        horizon_days=horizon_days,
        sample_count=len(samples),
        skipped_count=build.skipped,
        status=status,
        mean_forward_return_pct=sum(returns) / len(returns) if returns else None,
        directional_sample_count=len(directional),
        directional_accuracy=correct / len(directional) if directional else None,
        action_stats=action_stats,
        label_counts=dict(sorted(label_counts.items())),
        score_buckets=score_buckets,
        skip_reasons=dict(sorted(build.skip_reasons.items())),
        notes=notes,
    )


def calibrate_database(
    db_path: Path,
    *,
    horizon_days: int = 5,
    minimum_samples: int = 30,
) -> CalibrationReport:
    from bot.stock_db import StockDB

    db = StockDB.open(path=db_path)
    try:
        build = build_calibration_samples(
            db.fetch_all_rows("llm_analysis_history"),
            db.fetch_all_rows("price_history"),
            horizon_days=horizon_days,
        )
        return evaluate_calibration(
            build,
            horizon_days=horizon_days,
            minimum_samples=minimum_samples,
        )
    finally:
        db.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="評分訊號歷史校準 (只讀)")
    parser.add_argument("--db", default="", help="SQLite 路徑 (預設使用專案 stock.db)")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="專案資料根目錄（未指定 --db 時使用）",
    )
    parser.add_argument("--horizon", type=int, default=5, help="向前看幾個交易日")
    parser.add_argument("--minimum-samples", type=int, default=30)
    parser.add_argument("--json", default="", help="另存 JSON 報告")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    try:
        from bot.stock_db import default_db_path

        root = args.root.resolve() if args.root else Path.cwd()
        if args.db:
            db_path = Path(args.db)
            db_path = (
                (root / db_path).resolve()
                if not db_path.is_absolute()
                else db_path.resolve()
            )
        else:
            db_path = Path(default_db_path(root))
        report = calibrate_database(
            db_path,
            horizon_days=args.horizon,
            minimum_samples=args.minimum_samples,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"校準失敗：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.json:
        output_path = Path(args.json)
        if not output_path.is_absolute():
            output_path = root / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
    print(payload)
    return 0


__all__ = [
    "CalibrationReport",
    "CalibrationSample",
    "SampleBuildResult",
    "action_from_score",
    "build_calibration_samples",
    "calibrate_database",
    "evaluate_calibration",
]


if __name__ == "__main__":
    raise SystemExit(main())

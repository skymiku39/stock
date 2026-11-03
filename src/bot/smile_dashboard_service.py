"""Offline, reproducible dashboard runs; no broker, scheduler or LLM imports."""
from dataclasses import asdict, dataclass
from datetime import date
import json
import math
import hashlib
import sqlite3
from pathlib import Path
from uuid import uuid4

from bot.config import Settings
from bot.smile_curve_backtest import SmileCurveBacktester
from bot.smile_execution import SmileExecution
from bot.smile_snapshot import load_snapshot
from bot.stock_db import PriceBar, default_db_path
from bot.utils import now_tw

BASELINE = Path("data/reports/smile_execution_20261016")
REPORTS = Path("data/reports/smile_dashboard")


@dataclass(frozen=True)
class RunRequest:
    symbols: tuple[str, ...]
    start: str
    end: str
    capital: int = 500000
    slippage_pct: float = .1
    participation_pct: float = .1
    compare: bool = True
    stress_slippage_pct: float = .3
    stress_participation_pct: float = .01
    data_source: str = "snapshot"
    odd_lot: bool = False
    base_quantity: int = 1

    def validate(self):
        if self.data_source not in ("snapshot", "local_db"):
            raise ValueError("不支援的資料來源。")
        if not isinstance(self.base_quantity, int) or self.base_quantity < 1 or (self.odd_lot and self.base_quantity > 999):
            raise ValueError("基礎數量須為正整數，零股每筆最多 999 股。")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("請選擇至少一檔且不重複的股票。")
        if date.fromisoformat(self.start) > date.fromisoformat(self.end):
            raise ValueError("開始日期不能晚於結束日期。")
        if not math.isfinite(self.capital) or self.capital <= 0:
            raise ValueError("本金必須大於零。")
        for slip, volume in [(self.slippage_pct,self.participation_pct)] + (
                [(self.stress_slippage_pct,self.stress_participation_pct)] if self.compare else []):
            if not math.isfinite(slip) or not 0 <= slip < 100:
                raise ValueError("滑價必須介於 0% 至小於 100%。")
            if not math.isfinite(volume) or not 0 < volume <= 100:
                raise ValueError("成交量參與率必須大於 0% 且不超過 100%。")


def load_dashboard_inputs(root: Path, source: str):
    if source == "snapshot":
        return load_snapshot(root, root / BASELINE)
    if source != "local_db":
        raise ValueError("不支援的資料來源。")
    settings = Settings(_env_file=str(root / ".env"))
    symbols = sorted(set(settings.symbols or ["2330", "0050"]))
    symbols = [s for s in symbols if s == "0050" or
               (s.isdigit() and len(s) == 4 and not s.startswith("0"))]
    path = default_db_path(root).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("BEGIN")
        histories = {}
        for symbol in symbols:
            rows = connection.execute(
                "SELECT symbol,date,open,high,low,close,volume FROM price_history WHERE symbol=? ORDER BY date",
                (symbol,)).fetchall()
            if rows:
                histories[symbol] = [PriceBar(*row) for row in rows]
    finally:
        connection.close()
    if not histories:
        raise ValueError("本機資料庫沒有支援股票的日 K。")
    raw = json.dumps({s: [asdict(b) for b in rows] for s, rows in histories.items()},
                     sort_keys=True, ensure_ascii=False).encode("utf-8")
    return dict(input_sha256=hashlib.sha256(raw).hexdigest(), data_source="local_db"), histories


def run_comparison(root: Path, request: RunRequest) -> dict:
    request.validate()
    manifest, histories = load_dashboard_inputs(root, request.data_source)
    selected = {}
    for symbol in request.symbols:
        if symbol not in histories:
            raise ValueError(f"快照沒有 {symbol} 的資料。")
        rows = histories[symbol]
        if request.start < rows[0].date or request.end > rows[-1].date:
            raise ValueError(f"{symbol} 可用區間為 {rows[0].date}～{rows[-1].date}，請縮小日期範圍。")
        selected[symbol] = [b for b in rows if request.start <= b.date <= request.end]
        if len(selected[symbol]) < 2:
            raise ValueError(f"{symbol} 至少需要兩根日 K 才能回測次日成交。")
    settings = Settings(_env_file=None,max_fund=request.capital,daily_fund_budget=0,
                        smile_buy_tiers="1:1,3:2,5:3,8:4",smile_base_lot=request.base_quantity,
                        smile_reference_prices={},use_odd_lot=request.odd_lot,max_lot_per_symbol=2,
                        broker_fee_discount=.28,broker_min_fee=1,smile_regular_tax_rate=.003)
    scenarios = {"目前設定":SmileExecution(request.slippage_pct*100,request.participation_pct/100)}
    if request.compare:
        scenarios = {"理想成交":SmileExecution.ideal(), **scenarios,
                     "壓力情境":SmileExecution(request.stress_slippage_pct*100,request.stress_participation_pct/100)}
    results = {name:SmileCurveBacktester(settings,execution=model).run_histories(
        selected,start=request.start,end=request.end).to_dict() for name,model in scenarios.items()}
    return dict(schema_version=1,created_at=now_tw().isoformat(),request=asdict(request),
                input_sha256=manifest["input_sha256"],
                coverage={s:dict(start=rows[0].date,end=rows[-1].date,bars=len(rows)) for s,rows in selected.items()},
                results=results)


def save_run(root: Path, payload: dict) -> Path:
    directory = root / REPORTS
    directory.mkdir(parents=True,exist_ok=True)
    name = now_tw().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:10]
    target = directory / f"{name}.json"
    temp = directory / f"{name}.tmp"
    temp.write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    temp.replace(target)
    return target

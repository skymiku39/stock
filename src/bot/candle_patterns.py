"""candle_patterns -- 單根 K 棒型態辨識 (依「量化通」K 線教學的 16 種型態)。

設計
====
把一根 K 棒拆成四個量化特徵後分類：
* body            = |close - open|            (實體長度)
* total_range     = high - low                (整體長度)
* upper_shadow    = high - max(open, close)   (上影線)
* lower_shadow    = min(open, close) - low    (下影線)

再以「影線佔整體長度的比例」與「實體相對近期平均的大小」判定型態。

型態總表 (pattern_id)
====================
實體 K 線 (影線合計 ≤ 20%)：
    big_red / mid_red / small_red / big_black / mid_black / small_black
帶上影線 (短實體、無下影、上影 ≥ 2×實體)：
    inverted_hammer_red (墓碑線-上漲) / inverted_hammer_black (墓碑線-下跌)
帶下影線 (短實體、無上影、下影 ≥ 2×實體)：
    hammer_red (吊人線-上漲) / hammer_black (吊人線-下跌)
帶上下影線 (紡錘)：
    spinning_red / spinning_black
十字線 (開盤 ≈ 收盤)：
    doji (十字線) / dragonfly (T 字線) / gravestone (倒 T 線) / flat (一字線)

用法
====
```python
from bot.candle_patterns import classify_candle, classify_latest

p = classify_candle(open=100, high=110, low=99, close=109)
print(p.name, p.meaning, p.bias)

# 對含 OHLC 的 DataFrame 取最新一根 (自動以近 20 根平均實體判斷大/中/小)
p = classify_latest(df)
```
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


# ----------------------------------------------------------------------
# 參數 (可微調)
# ----------------------------------------------------------------------

DOJI_BODY_RATIO = 0.05      # 實體 / 整體 ≤ 5% 視為「開盤≈收盤」(十字家族)
FLAT_RANGE_EPS = 1e-9       # high≈low 視為一字線
ENTITY_SHADOW_MAX = 0.20    # 影線合計 ≤ 20% → 實體 K 線
TINY_SHADOW_RATIO = 0.10    # 單側影線 ≤ 10% 視為「幾乎沒有影線」
LONG_SHADOW_RATIO = 0.60    # 單側影線 ≥ 60% → 長影 (十字家族判 T / 倒 T)
HAMMER_SHADOW_MULT = 2.0    # 影線 ≥ 2×實體 → 鎚子 / 倒鎚
HAMMER_BODY_MAX = 0.40      # 鎚子家族的實體上限 (佔整體)
SPINNING_BODY_MAX = 0.50    # 紡錘的實體上限 (佔整體)

# 大/中/小 實體：以「實體 / 近期平均實體」分級 (avg_body 可用時)
BIG_BODY_MULT = 1.3
SMALL_BODY_MULT = 0.6
# 無 avg_body 時，退回「實體 / 收盤價」百分比分級
BIG_BODY_PRICE_PCT = 0.040
SMALL_BODY_PRICE_PCT = 0.015


# ----------------------------------------------------------------------
# 資料模型
# ----------------------------------------------------------------------


@dataclass
class CandlePattern:
    """單根 K 棒型態判定結果。"""

    pattern_id: str
    name: str                       # 中文型態名
    category: str                   # 實體 / 上影線 / 下影線 / 上下影線 / 十字線
    color: str                      # red / black / doji
    bias: Optional[bool]            # True=偏多, False=偏空, None=中性/取決於位置
    reversal: bool                  # 是否屬潛在反轉型態
    strength: str                   # strong / medium / weak
    meaning: str                    # 市場訊號 (中文一句話)
    body_pct: float                 # 實體 / 整體
    upper_pct: float                # 上影線 / 整體
    lower_pct: float                # 下影線 / 整體

    @property
    def emoji(self) -> str:
        if self.bias is True:
            return "🔴"
        if self.bias is False:
            return "🟢"
        return "⚪"


# 型態靜態屬性表：pattern_id -> (name, category, color, bias, reversal, strength, meaning)
_META: Dict[str, tuple] = {
    # ---- 實體 K 線 ----
    "big_red": ("大紅K", "實體", "red", True, False, "strong",
                "多頭強勢、大量買方進駐，後市看漲。"),
    "mid_red": ("中紅K", "實體", "red", True, False, "medium",
                "買方明顯佔優勢 (未到大紅K)，多數時候可視為反轉訊號。"),
    "small_red": ("小紅K", "實體", "red", True, False, "weak",
                  "買方略勝賣方，常在盤整趨勢中出現。"),
    "big_black": ("大黑K", "實體", "black", False, False, "strong",
                  "空頭強勢、大量賣方拋售，後市看跌。"),
    "mid_black": ("中黑K", "實體", "black", False, False, "medium",
                  "賣方明顯佔優勢 (未到大黑K)，多數時候可視為反轉訊號。"),
    "small_black": ("小黑K", "實體", "black", False, False, "weak",
                    "賣方略勝買方，常在盤整趨勢中出現。"),
    # ---- 帶上影線 ----
    "inverted_hammer_red": ("倒鎚紅K (墓碑線–上漲)", "上影線", "red", False, True, "medium",
                            "買方開盤強勢但被賣方壓制、未能突破，是潛在反轉訊號。"),
    "inverted_hammer_black": ("倒鎚黑K (墓碑線–下跌)", "上影線", "black", False, True, "medium",
                              "盤中拉高被壓回收跌，後續下跌可能性高。"),
    # ---- 帶下影線 ----
    "hammer_red": ("紅K鎚子 (吊人線–上漲)", "下影線", "red", True, True, "medium",
                   "開盤殺低被買盤承接拉升、收高於開盤，後續上漲可能性高。"),
    "hammer_black": ("黑K鎚子 (吊人線–下跌)", "下影線", "black", False, True, "medium",
                     "開盤強勢下殺，雖有買盤接手但賣方略勝，收低於開盤。"),
    # ---- 帶上下影線 (紡錘) ----
    "spinning_red": ("紡錘紅K", "上下影線", "red", True, False, "weak",
                     "多空交戰後買方勝出；實體越短越勢均力敵。"),
    "spinning_black": ("紡錘黑K", "上下影線", "black", False, False, "weak",
                       "多空交戰後賣方勝出；實體越短越勢均力敵。"),
    # ---- 十字線 ----
    "doji": ("十字線", "十字線", "doji", None, True, "weak",
             "多空勢均力敵；高檔出現可能轉空、低檔出現可能轉多。"),
    "dragonfly": ("T 字線", "十字線", "doji", None, True, "weak",
                  "開低被買盤拉回；高檔代表買方疲乏、低檔代表空方即將結束。"),
    "gravestone": ("倒 T 線", "十字線", "doji", None, True, "weak",
                   "拉高被大量賣壓壓回；高檔出現代表多頭即將結束。"),
    "flat": ("一字線", "十字線", "doji", None, False, "weak",
             "不常見的極端行情，多出現於漲停 / 跌停 / 無量。"),
}


# ----------------------------------------------------------------------
# 核心分類
# ----------------------------------------------------------------------


def _build(pattern_id: str, *, body_pct: float, upper_pct: float, lower_pct: float) -> CandlePattern:
    name, category, color, bias, reversal, strength, meaning = _META[pattern_id]
    return CandlePattern(
        pattern_id=pattern_id,
        name=name,
        category=category,
        color=color,
        bias=bias,
        reversal=reversal,
        strength=strength,
        meaning=meaning,
        body_pct=round(body_pct, 4),
        upper_pct=round(upper_pct, 4),
        lower_pct=round(lower_pct, 4),
    )


def _entity_size(body: float, close: float, avg_body: Optional[float]) -> str:
    """判定實體大小 → 'big' / 'mid' / 'small'。"""
    if avg_body and avg_body > 0:
        ratio = body / avg_body
        if ratio >= BIG_BODY_MULT:
            return "big"
        if ratio <= SMALL_BODY_MULT:
            return "small"
        return "mid"
    # 退回：實體佔收盤價百分比
    pct = body / close if close else 0.0
    if pct >= BIG_BODY_PRICE_PCT:
        return "big"
    if pct <= SMALL_BODY_PRICE_PCT:
        return "small"
    return "mid"


def classify_candle(
    *,
    open: float,
    high: float,
    low: float,
    close: float,
    avg_body: Optional[float] = None,
) -> CandlePattern:
    """把單根 K 棒分類為 16 種型態之一。

    Args:
        open/high/low/close: 該根 K 棒的開高低收。
        avg_body: 近期平均實體長度 (用於判斷大/中/小)；None 時退回實體佔價百分比。
    """
    o, h, l, c = float(open), float(high), float(low), float(close)
    rng = h - l

    # 一字線：高低幾乎相同
    if rng <= FLAT_RANGE_EPS:
        return _build("flat", body_pct=0.0, upper_pct=0.0, lower_pct=0.0)

    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    body_pct = body / rng
    upper_pct = upper / rng
    lower_pct = lower / rng
    is_red = c >= o

    # ---- 十字家族：實體極小 (開盤≈收盤) ----
    if body_pct <= DOJI_BODY_RATIO:
        has_upper = upper_pct > TINY_SHADOW_RATIO
        has_lower = lower_pct > TINY_SHADOW_RATIO
        if not has_upper and not has_lower:
            # 影線都極短 → 視為一字線 (極端無波動)
            return _build("flat", body_pct=body_pct, upper_pct=upper_pct, lower_pct=lower_pct)
        if lower_pct >= LONG_SHADOW_RATIO and upper_pct <= TINY_SHADOW_RATIO:
            return _build("dragonfly", body_pct=body_pct, upper_pct=upper_pct, lower_pct=lower_pct)
        if upper_pct >= LONG_SHADOW_RATIO and lower_pct <= TINY_SHADOW_RATIO:
            return _build("gravestone", body_pct=body_pct, upper_pct=upper_pct, lower_pct=lower_pct)
        return _build("doji", body_pct=body_pct, upper_pct=upper_pct, lower_pct=lower_pct)

    has_upper = upper_pct > TINY_SHADOW_RATIO
    has_lower = lower_pct > TINY_SHADOW_RATIO

    # ---- 鎚子 / 倒鎚 (短實體、單側長影 ≥ 2×實體) ----
    if body_pct < HAMMER_BODY_MAX:
        # 倒鎚 (墓碑)：無下影、上影 ≥ 2×實體
        if not has_lower and upper_pct >= HAMMER_SHADOW_MULT * body_pct:
            pid = "inverted_hammer_red" if is_red else "inverted_hammer_black"
            return _build(pid, body_pct=body_pct, upper_pct=upper_pct, lower_pct=lower_pct)
        # 鎚子 (吊人)：無上影、下影 ≥ 2×實體
        if not has_upper and lower_pct >= HAMMER_SHADOW_MULT * body_pct:
            pid = "hammer_red" if is_red else "hammer_black"
            return _build(pid, body_pct=body_pct, upper_pct=upper_pct, lower_pct=lower_pct)

    # ---- 紡錘：上下都有明顯影線、實體偏短 ----
    if has_upper and has_lower and body_pct < SPINNING_BODY_MAX:
        pid = "spinning_red" if is_red else "spinning_black"
        return _build(pid, body_pct=body_pct, upper_pct=upper_pct, lower_pct=lower_pct)

    # ---- 實體 K 線：影線合計小 (或實體主導) ----
    size = _entity_size(body, c, avg_body)
    color = "red" if is_red else "black"
    pid = f"{size}_{color}"
    return _build(pid, body_pct=body_pct, upper_pct=upper_pct, lower_pct=lower_pct)


# ----------------------------------------------------------------------
# DataFrame 便利介面
# ----------------------------------------------------------------------


def _avg_body_from_rows(rows: List[Dict[str, float]], window: int = 20) -> Optional[float]:
    bodies = [abs(float(r["close"]) - float(r["open"])) for r in rows[-window:]]
    bodies = [b for b in bodies if b > 0]
    if not bodies:
        return None
    return sum(bodies) / len(bodies)


def classify_latest(df, *, window: int = 20) -> Optional[CandlePattern]:
    """對含 open/high/low/close 欄位的 DataFrame，分類最新一根 K 棒。

    以最近 ``window`` 根的平均實體做大/中/小判斷。
    """
    if df is None or len(df) == 0:
        return None
    cols = {"open", "high", "low", "close"}
    if not cols.issubset(set(df.columns)):
        return None
    rows = df.tail(window)[["open", "high", "low", "close"]].to_dict("records")
    if not rows:
        return None
    avg_body = _avg_body_from_rows(rows, window)
    last = rows[-1]
    return classify_candle(
        open=last["open"], high=last["high"],
        low=last["low"], close=last["close"],
        avg_body=avg_body,
    )


def classify_recent(df, *, n: int = 5, window: int = 20) -> List[Dict[str, Any]]:
    """回傳最近 ``n`` 根 K 棒的型態 (含日期)，由新到舊。"""
    if df is None or len(df) == 0:
        return None or []
    cols = {"open", "high", "low", "close"}
    if not cols.issubset(set(df.columns)):
        return []
    has_date = "date" in df.columns
    all_rows = df[["open", "high", "low", "close"] + (["date"] if has_date else [])].to_dict("records")
    out: List[Dict[str, Any]] = []
    total = len(all_rows)
    for i in range(total - 1, max(-1, total - 1 - n), -1):
        win = all_rows[max(0, i - window + 1): i + 1]
        avg_body = _avg_body_from_rows(win, window)
        r = all_rows[i]
        p = classify_candle(
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            avg_body=avg_body,
        )
        item = pattern_to_dict(p)
        if has_date:
            item["date"] = str(r.get("date", ""))
        out.append(item)
    return out


def pattern_to_dict(p: Optional[CandlePattern]) -> Dict[str, Any]:
    if p is None:
        return {}
    return asdict(p)


__all__ = [
    "CandlePattern",
    "classify_candle",
    "classify_latest",
    "classify_recent",
    "pattern_to_dict",
]

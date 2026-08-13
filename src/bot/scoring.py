"""scoring -- 個股「分析量表」系統。

設計目標
========
讓使用者能在畫面上直接看到：
1. 每檔股票在四個時間框架 (當沖 / 短期 / 中期 / 長期) 的綜合分數
2. 每個分數由哪些 factor 加權而來、各自的明細
3. 對應到具體的操作建議 (買進 / 觀望 / 賣出) 與進場/停損/停利

Factor (0-100 分)
=================
* llm_sentiment   -- 法說語意分數 (Gemini 解析後的 sentiment_score, confidence)
* logic           -- 言行一致性 (consistent / suspicious_distribution / ...)
* etf_consensus   -- 主動式 ETF 共識強度 (持有檔數 + 新建倉/加碼訊號)
* chips           -- 籌碼面方向 (三大法人 + 借券/融資變動)
* technical       -- 技術面 (MA/MACD/RSI/KD/量比 — 由 technicals.TechnicalSnapshot 提供)
* fundamental     -- 基本面 (月營收 YoY 連續、PER 合理、ROE/三率) 0-100
* distribution    -- 大戶 vs 散戶結構 (TDCC 集保) 0-100
* us_market       -- 美股連動 (對應供應鏈夥伴當夜表現 + SOX/NASDAQ 大勢)
* risk            -- 風險警示 (借券暴增、融資爆量、法說提到的 risk)

每個時間框架的加權不同 (詳見 `WEIGHTS`)；缺資料的 factor 會自動扣權重 + 給中性 50 分。

命名對照
========
* `ticker`（本模組參數名）≡ `symbol`（交易核心）
* `pct_change`（本模組參數名）≡ `pct_chg`（`MarketTick`）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ----------------------------------------------------------------------
# 常數
# ----------------------------------------------------------------------

TIMEFRAMES: list[str] = ["day_trade", "short_term", "mid_term", "long_term"]

TIMEFRAME_LABELS: dict[str, str] = {
    "day_trade": "當沖 (Intraday)",
    "short_term": "短期 (1-2 週)",
    "mid_term": "中期 (1-3 月)",
    "long_term": "長期 (>3 月)",
}

# 各時間框架的 factor 權重 (總和接近 1.0)
WEIGHTS: dict[str, dict[str, float]] = {
    # 當沖最看重「美股夜盤＋費半」對當日開盤的影響
    "day_trade": {
        "technical": 0.42,
        "us_market": 0.18,
        "chips": 0.18,
        "distribution": 0.07,
        "etf_consensus": 0.05,
        "risk": 0.10,
    },
    "short_term": {
        "technical": 0.22,
        "us_market": 0.15,
        "chips": 0.18,
        "distribution": 0.10,
        "etf_consensus": 0.12,
        "fundamental": 0.10,
        "llm_sentiment": 0.08,
        "risk": 0.05,
    },
    "mid_term": {
        "fundamental": 0.20,
        "llm_sentiment": 0.16,
        "logic": 0.14,
        "etf_consensus": 0.16,
        "us_market": 0.10,
        "distribution": 0.08,
        "chips": 0.06,
        "technical": 0.05,
        "risk": 0.05,
    },
    "long_term": {
        "fundamental": 0.28,
        "llm_sentiment": 0.22,
        "logic": 0.18,
        "etf_consensus": 0.17,
        "us_market": 0.05,
        "distribution": 0.05,
        "risk": 0.05,
    },
}

# 進場/停損/停利規則 (依時間框架，% 為相對 entry 的變動)
# 僅供 Dashboard / 簡報參考；bot 實際執行見 STOP_LOSS_PCT、TAKE_PROFIT_PCT 等 env 設定。
ADVISORY_RULE_NOTE = (
    "以下停損/停利為評分建議值，非 bot 自動執行參數。"
    "Bot 預設：停損 -3%（淨利）、移動停利 +6% 後回撤 2%。"
)

STRATEGY_RULES: dict[str, dict[str, Any]] = {
    "day_trade": {
        "stop_pct": -1.0,
        "target_pct": 2.0,
        "size_hint": "1 張",
        "horizon": "當日收盤前必平",
        "entry_logic": "開盤後追量；近 VWAP 介入",
    },
    "short_term": {
        "stop_pct": -4.0,
        "target_pct": 8.0,
        "size_hint": "1-2 張 (分批)",
        "horizon": "1-2 週",
        "entry_logic": "拉回不破 5 日線進場；量價背離出場",
    },
    "mid_term": {
        "stop_pct": -8.0,
        "target_pct": 20.0,
        "size_hint": "2-3 張 (分批)",
        "horizon": "1-3 個月",
        "entry_logic": "確認月線方向 + 主力連續進駐後分批佈局",
    },
    "long_term": {
        "stop_pct": -15.0,
        "target_pct": 40.0,
        "size_hint": "3-5 張 (定期分批)",
        "horizon": ">3 個月",
        "entry_logic": "結構性題材 (Capex 擴張/毛利率上行) + ETF 持續加碼為核心理由",
    },
}


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------


@dataclass
class FactorScore:
    """單一 factor 的評分結果。"""

    key: str
    label: str
    score: float           # 0-100，未取得資料時給 50 並 available=False
    weight: float = 0.0    # 當前時間框架的權重
    detail: str = ""
    available: bool = True
    sub_scores: dict[str, float] = field(default_factory=dict)


@dataclass
class TimeframeScore:
    timeframe: str
    label: str
    total: float          # 0-100 加權平均
    action: str           # STRONG_BUY / BUY / HOLD / REDUCE / SELL
    action_label: str
    color: str            # for UI badge
    confidence: float     # 0-1，依 factor 可用比例 + LLM confidence
    factors: list[FactorScore] = field(default_factory=list)
    strategy: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class StockScorecard:
    ticker: str
    name: str = ""
    price: float = 0.0
    pct_change: float = 0.0
    volume: float = 0.0
    timeframes: dict[str, TimeframeScore] = field(default_factory=dict)
    summary_notes: list[str] = field(default_factory=list)
    fetched_at: str = ""

    def best_action(self) -> str:
        """回傳所有時間框架中最積極的建議 (供 watchlist 摘要顯示)。"""
        priority = {"STRONG_BUY": 4, "BUY": 3, "HOLD": 2, "REDUCE": 1, "SELL": 0}
        best = "HOLD"
        for tf in self.timeframes.values():
            if priority.get(tf.action, 2) > priority.get(best, 2):
                best = tf.action
        return best


# ----------------------------------------------------------------------
# Factor 計算
# ----------------------------------------------------------------------


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def factor_llm_sentiment(analysis: dict[str, Any] | None) -> FactorScore:
    if not analysis:
        return FactorScore(
            "llm_sentiment", "法說語意", 50.0,
            detail="尚無 LLM 法說分析", available=False,
        )
    sent = float(analysis.get("sentiment_score", 0) or 0)
    conf = float(analysis.get("confidence", 0.5) or 0.5)
    raw = 50.0 + sent * 50.0
    blended = raw * conf + 50.0 * (1.0 - conf)
    return FactorScore(
        "llm_sentiment", "法說語意",
        score=_clamp(blended),
        detail=f"sentiment={sent:+.2f}, confidence={conf:.0%}",
        sub_scores={"sentiment_raw": raw, "confidence_blend": blended},
    )


def factor_logic(logic: dict[str, Any] | None) -> FactorScore:
    if not logic:
        return FactorScore(
            "logic", "言行一致性", 50.0,
            detail="尚無言行反查", available=False,
        )
    verdict = str(logic.get("verdict", "inconclusive"))
    conf = float(logic.get("confidence", 0.4) or 0.4)
    mapping = {
        "consistent": 75.0,                # 預設正向；suggestion=avoid 時下面會修正
        "suspicious_accumulation": 72.0,
        "suspicious_distribution": 15.0,
        "inconclusive": 50.0,
    }
    base = mapping.get(verdict, 50.0)
    if verdict == "consistent" and logic.get("suggestion") == "avoid":
        base = 25.0
    blended = base * conf + 50.0 * (1.0 - conf)
    return FactorScore(
        "logic", "言行一致性",
        score=_clamp(blended),
        detail=f"verdict={verdict}, suggestion={logic.get('suggestion','-')}, conf={conf:.0%}",
    )


def factor_etf_consensus(
    consensus: dict[str, Any] | None,
    new_build: dict[str, Any] | None,
    add: dict[str, Any] | None,
) -> FactorScore:
    etf_count = 0
    total_weight = 0.0
    if consensus:
        etf_count = int(consensus.get("etf_count", 0) or 0)
        total_weight = float(consensus.get("total_weight", 0) or 0)

    if etf_count == 0 and not new_build and not add:
        return FactorScore(
            "etf_consensus", "ETF 共識", 50.0,
            detail="無主動 ETF 持有", available=False,
        )

    base = 50.0
    if etf_count >= 1:
        base += min(etf_count * 7.0, 30.0)
    if total_weight >= 5.0:
        base += min((total_weight - 5.0) * 0.5, 5.0)
    bonus = 0.0
    if new_build:
        bonus += 10.0
    if add:
        bonus += 5.0
    score = _clamp(base + bonus)
    parts = [f"{etf_count} 檔 ETF 持有 (合計權重 {total_weight:.1f}%)"]
    if new_build:
        parts.append(f"共識新建倉 ({new_build.get('etf_count', '?')} 檔)")
    if add:
        parts.append(f"共識加碼 ({add.get('etf_count', '?')} 檔)")
    return FactorScore(
        "etf_consensus", "ETF 共識",
        score=score, detail="；".join(parts),
        sub_scores={"etf_count": float(etf_count), "total_weight": total_weight},
    )


def factor_chips(chips: dict[str, Any] | None) -> FactorScore:
    """chips 是 chips_fetcher.summary_to_dict() 的輸出。"""
    if not chips:
        return FactorScore(
            "chips", "籌碼方向", 50.0,
            detail="尚無籌碼面資料", available=False,
        )
    foreign = float(chips.get("foreign_net", 0) or 0)
    trust = float(chips.get("investment_trust_net", 0) or 0)
    dealer = float(chips.get("dealer_net", 0) or 0)
    borrow_pct = float(chips.get("short_borrow_change_pct", 0) or 0)
    margin_pct = float(chips.get("margin_buy_change_pct", 0) or 0)
    block = float(chips.get("block_trade_net", 0) or 0)

    # 外資 + 投信合計 (對小台股 1000 張很大；對台積電 1000 張很小)
    smart_money = foreign + trust + 0.3 * dealer
    smart_norm = max(-1.0, min(1.0, smart_money / 5000.0))
    base = 50.0 + smart_norm * 35.0

    # 借券暴增扣分；融資爆量 (>15%) 扣分；鉅額交易視為中性
    if borrow_pct > 20.0:
        base -= 15.0
    elif borrow_pct > 10.0:
        base -= 7.0
    if margin_pct > 15.0:
        base -= 8.0
    if block > 5000:
        base += 3.0

    score = _clamp(base)
    return FactorScore(
        "chips", "籌碼方向",
        score=score,
        detail=(
            f"外資 {foreign:+.0f} 張、投信 {trust:+.0f}、自營 {dealer:+.0f}；"
            f"借券變動 {borrow_pct:+.1f}%、融資 {margin_pct:+.1f}%"
        ),
        sub_scores={
            "smart_money": smart_money, "borrow_pct": borrow_pct,
            "margin_pct": margin_pct, "block": block,
        },
    )


def factor_technical(
    price: float,
    pct_change: float,
    volume: float = 0,
    technical_snapshot: dict[str, Any] | None = None,
) -> FactorScore:
    """技術面 factor — 優先用 TechnicalSnapshot 算出的指標分數。"""
    if technical_snapshot and technical_snapshot.get("rows", 0) > 0:
        score = float(technical_snapshot.get("technical_score", 50.0) or 50.0)
        bits = []
        if technical_snapshot.get("ma60") and technical_snapshot.get("last_close"):
            ma60 = technical_snapshot["ma60"]
            close = technical_snapshot["last_close"]
            bits.append(f"季線 {ma60:.2f} {'≥' if close >= ma60 else '<'} 收盤 {close:.2f}")
        if technical_snapshot.get("rsi14") is not None:
            bits.append(f"RSI14={technical_snapshot['rsi14']:.1f}")
        if technical_snapshot.get("macd_hist") is not None:
            bits.append(f"MACD柱={technical_snapshot['macd_hist']:.2f}")
        if technical_snapshot.get("pct_change_20d") is not None:
            bits.append(f"20日 {technical_snapshot['pct_change_20d']:+.1f}%")
        detail = "；".join(bits) if bits else "技術指標已計算"
        return FactorScore(
            "technical", "技術面",
            score=_clamp(score),
            detail=detail,
            sub_scores={
                "pct_change_1d": technical_snapshot.get("pct_change_1d", 0.0),
                "pct_change_20d": technical_snapshot.get("pct_change_20d", 0.0),
                "rsi14": technical_snapshot.get("rsi14") or 50.0,
            },
        )

    if price <= 0 and pct_change == 0:
        return FactorScore(
            "technical", "技術面", 50.0,
            detail="尚無報價資料", available=False,
        )
    # 漲跌幅: 對映到 0-100 (沒抓到日 K 時的 fallback)
    if pct_change >= 5:
        ts = 90.0
    elif pct_change >= 2:
        ts = 60.0 + (pct_change - 2) * 10.0
    elif pct_change >= 0:
        ts = 50.0 + pct_change * 5.0
    elif pct_change >= -2:
        ts = 50.0 + pct_change * 7.5
    elif pct_change >= -5:
        ts = 35.0 + (pct_change + 2) * 5.0
    else:
        ts = 15.0
    score = _clamp(ts)
    return FactorScore(
        "technical", "技術面",
        score=score,
        detail=f"漲跌幅 {pct_change:+.2f}% (僅有單日漲跌資料)",
        sub_scores={"pct_change": pct_change},
    )


def factor_fundamental(fundamental: dict[str, Any] | None) -> FactorScore:
    """基本面 factor — 看月營收連續性、PER、ROE/三率 (有資料才加分)。"""
    if not fundamental:
        return FactorScore(
            "fundamental", "基本面", 50.0,
            detail="尚無基本面資料", available=False,
        )
    revs = fundamental.get("revenues") or []
    val = fundamental.get("valuation") or {}
    quarterlies = fundamental.get("quarterlies") or []
    derived = fundamental.get("derived") or {}

    score = 50.0
    bits: list[str] = []
    score_sub: dict[str, float] = {}

    # 1) 月營收 YoY 連續性
    yoy_streak = int(derived.get("revenue_yoy_streak") or 0)
    if yoy_streak >= 6:
        score += 12.0
    elif yoy_streak >= 3:
        score += 6.0
    elif yoy_streak >= 1:
        score += 2.0
    if revs:
        latest = derived.get("latest_revenue") or {}
        if latest:
            yoy = float(latest.get("yoy", 0))
            mom = float(latest.get("mom", 0))
            bits.append(
                f"{latest.get('year','-')}/{latest.get('month','-')} 月營收 YoY {yoy:+.1f}%、MoM {mom:+.1f}%"
                + (f" (連 {yoy_streak} 個月正成長)" if yoy_streak >= 2 else "")
            )
            score_sub["latest_yoy"] = yoy
            score_sub["yoy_streak"] = float(yoy_streak)
            # YoY > 30% 額外加分
            if yoy >= 30:
                score += 6.0
            elif yoy >= 10:
                score += 3.0
            elif yoy <= -10:
                score -= 6.0

    # 2) 估值 (PER 合理區)
    if val:
        pe = float(val.get("pe_ratio") or 0)
        pb = float(val.get("pb_ratio") or 0)
        dy = float(val.get("dividend_yield") or 0)
        if 5 <= pe <= 20:
            score += 6.0
        elif 20 < pe <= 35:
            score += 2.0
        elif pe > 50 and pe > 0:
            score -= 6.0
        if dy >= 5:
            score += 5.0
        elif dy >= 3:
            score += 2.0
        if 0 < pb <= 2:
            score += 2.0
        elif pb > 5:
            score -= 3.0
        bits.append(f"PER={pe:.1f}, PBR={pb:.2f}, 殖利率={dy:.2f}%")
        score_sub.update({"pe": pe, "pb": pb, "dy": dy})

    # 3) ROE / 毛利率 (季報)
    if quarterlies:
        latest_q = max(quarterlies, key=lambda q: (q.get("year", 0), q.get("quarter", 0)))
        gm = float(latest_q.get("gross_margin") or 0)
        nm = float(latest_q.get("net_margin") or 0)
        roe = latest_q.get("roe")
        if gm >= 40:
            score += 5.0
        elif gm >= 25:
            score += 2.0
        elif gm and gm < 10:
            score -= 4.0
        if roe and roe >= 15:
            score += 8.0
        elif roe and roe >= 10:
            score += 4.0
        bits.append(
            f"最新季 Q{latest_q.get('quarter','-')} EPS={latest_q.get('eps','-')}、毛利率={gm:.1f}%、淨利率={nm:.1f}%"
            + (f"、ROE={roe:.1f}%" if roe else "")
        )
        score_sub["gross_margin"] = gm
        score_sub["net_margin"] = nm
        if roe:
            score_sub["roe"] = float(roe)

    return FactorScore(
        "fundamental", "基本面",
        score=_clamp(score),
        detail="；".join(bits) if bits else "資料不足",
        sub_scores=score_sub,
    )


def factor_distribution(
    distribution_label: str = "",
    distribution_detail: str = "",
    distribution_score: float = 50.0,
    available: bool = True,
) -> FactorScore:
    """大戶 vs 散戶結構分數（來自 TDCC 集保戶分散表）。"""
    if not available or not distribution_label:
        return FactorScore(
            "distribution", "大戶結構", 50.0,
            detail="尚無 TDCC 資料", available=False,
        )
    return FactorScore(
        "distribution", "大戶結構",
        score=_clamp(distribution_score),
        detail=distribution_detail or distribution_label,
        sub_scores={"raw_score": distribution_score},
    )


def factor_us_market(
    macro: dict[str, Any] | None = None,
    related_us: list[dict[str, Any]] | None = None,
    adr_premium: dict[str, Any] | None = None,
) -> FactorScore:
    """美股連動 factor。

    參數：
    * macro      — market_macro.macro_to_dict() 的輸出 (含 indices + stocks)
    * related_us — 從 supply_chain 反查到的 [{us_ticker, weight, role, ...}] 清單
    * adr_premium— 若此股自己就有 ADR (例如 2330 → TSM)，傳入 AdrPremium dict
    """
    if not macro:
        return FactorScore(
            "us_market", "美股連動", 50.0,
            detail="尚無美股資料 (請先跑 macro 抓取)", available=False,
        )

    indices = macro.get("indices") or {}
    stocks = macro.get("stocks") or {}

    base = 50.0
    detail_bits: list[str] = []
    sub: dict[str, float] = {}

    # 1) 供應鏈夥伴 (含 ADR 母股自身)
    weighted_sum = 0.0
    weight_total = 0.0
    related_quotes: list[str] = []
    for r in (related_us or []):
        us_sym = r.get("us_ticker") or r.get("us_symbol")
        if not us_sym:
            continue
        q = stocks.get(us_sym)
        if q is None:
            continue
        pct = float(q.get("pct_change", 0) or 0)
        w = float(r.get("weight", 0.5) or 0.5)
        weighted_sum += pct * w
        weight_total += w
        related_quotes.append(f"{us_sym} {pct:+.1f}%")
    if weight_total > 0:
        avg_pct = weighted_sum / weight_total
        sub["supply_chain_avg_pct"] = avg_pct
        # 強敏感度：+5% → +30 分；-5% → -30 分
        base += max(-30.0, min(30.0, avg_pct * 6.0))
        detail_bits.append(
            f"供應鏈夥伴 {len(related_quotes)} 檔平均 {avg_pct:+.2f}% "
            f"({', '.join(related_quotes[:3])})"
        )

    # 2) 費半 SOX (對所有半導體股都有影響，權重稍小)
    sox = indices.get("^SOX")
    if sox:
        sox_pct = float(sox.get("pct_change", 0) or 0)
        sub["sox_pct"] = sox_pct
        # 沒有供應鏈資料時 SOX 起主要作用，否則只小幅微調
        sox_weight = 4.0 if weight_total == 0 else 1.5
        base += max(-15.0, min(15.0, sox_pct * sox_weight))
        detail_bits.append(f"SOX {sox_pct:+.2f}%")

    # 3) VIX 過高 → 風險偏好下降
    vix = indices.get("^VIX")
    if vix:
        vix_price = float(vix.get("price", 0) or 0)
        sub["vix"] = vix_price
        if vix_price > 30:
            base -= 10.0
            detail_bits.append(f"VIX={vix_price:.1f} (極度恐慌)")
        elif vix_price > 22:
            base -= 4.0
            detail_bits.append(f"VIX={vix_price:.1f} (升溫)")
        elif vix_price > 0:
            detail_bits.append(f"VIX={vix_price:.1f}")

    # 4) ADR 溢價 — 強烈當沖訊號
    if adr_premium:
        prem_pct = float(adr_premium.get("premium_pct", 0) or 0)
        sub["adr_premium_pct"] = prem_pct
        # 公允台股價 > 現價 → ADR 溢價 → 多
        base += max(-12.0, min(12.0, prem_pct * 1.5))
        detail_bits.append(
            f"ADR 溢價 {prem_pct:+.2f}% (公允 {adr_premium.get('fair_tw_price','-')})"
        )

    if not detail_bits:
        return FactorScore(
            "us_market", "美股連動", 50.0,
            detail="此股無對應美股資料；可在 supply_chain.json 中設定",
            available=False, sub_scores=sub,
        )

    return FactorScore(
        "us_market", "美股連動",
        score=_clamp(base),
        detail="；".join(detail_bits),
        sub_scores=sub,
    )


def factor_risk(
    chips: dict[str, Any] | None,
    analysis: dict[str, Any] | None,
) -> FactorScore:
    score = 70.0
    notes: list[str] = []
    if chips:
        borrow_pct = float(chips.get("short_borrow_change_pct", 0) or 0)
        margin_pct = float(chips.get("margin_buy_change_pct", 0) or 0)
        if borrow_pct > 25:
            score -= 30
            notes.append(f"借券暴增 +{borrow_pct:.1f}%")
        elif borrow_pct > 10:
            score -= 12
            notes.append(f"借券增加 +{borrow_pct:.1f}%")
        if margin_pct > 20:
            score -= 20
            notes.append(f"融資爆量 +{margin_pct:.1f}%")
        elif margin_pct > 10:
            score -= 8
            notes.append(f"融資增加 +{margin_pct:.1f}%")
    available = bool(chips or analysis)
    if analysis:
        risks = analysis.get("risks") or []
        risk_text = "; ".join(map(str, risks)).lower()
        flags = ["訴訟", "下修", "降評", "減資", "罰款", "罷工", "斷供", "客戶流失"]
        for f in flags:
            if f in risk_text:
                score -= 8
                notes.append(f"LLM 標記風險: {f}")
    if not notes:
        notes.append("無重大風險警示")
    return FactorScore(
        "risk", "風險警示",
        score=_clamp(score),
        detail="；".join(notes),
        available=available,
    )


# ----------------------------------------------------------------------
# 加權合成
# ----------------------------------------------------------------------


def _action_from_score(score: float) -> tuple[str, str, str]:
    if score >= 78:
        return "STRONG_BUY", "強烈買進", "green"
    if score >= 62:
        return "BUY", "買進", "green"
    if score >= 45:
        return "HOLD", "觀望", "gray"
    if score >= 30:
        return "REDUCE", "減碼", "orange"
    return "SELL", "賣出", "red"


def _build_strategy(timeframe: str, action: str, price: float) -> dict[str, Any]:
    rules = STRATEGY_RULES.get(timeframe, {})
    if price <= 0:
        return {"applicable": False, "note": "缺現價，無法計算進出場價"}
    stop = round(price * (1.0 + rules["stop_pct"] / 100.0), 2)
    target = round(price * (1.0 + rules["target_pct"] / 100.0), 2)
    return {
        "applicable": action in ("STRONG_BUY", "BUY"),
        "entry": price,
        "stop": stop,
        "stop_pct": rules["stop_pct"],
        "target": target,
        "target_pct": rules["target_pct"],
        "rrr": round(abs(rules["target_pct"] / rules["stop_pct"]), 2),
        "size_hint": rules["size_hint"],
        "horizon": rules["horizon"],
        "entry_logic": rules["entry_logic"],
    }


def compute_scorecard(
    *,
    ticker: str,
    name: str = "",
    price: float = 0.0,
    pct_change: float = 0.0,
    volume: float = 0.0,
    llm_analysis: dict[str, Any] | None = None,
    logic_result: dict[str, Any] | None = None,
    consensus: dict[str, Any] | None = None,
    new_build_signal: dict[str, Any] | None = None,
    add_signal: dict[str, Any] | None = None,
    chip_summary: dict[str, Any] | None = None,
    fundamental: dict[str, Any] | None = None,
    technical_snapshot: dict[str, Any] | None = None,
    distribution_label: str = "",
    distribution_detail: str = "",
    distribution_score: float = 50.0,
    has_distribution: bool = False,
    macro_snapshot: dict[str, Any] | None = None,
    related_us_stocks: list[dict[str, Any]] | None = None,
    adr_premium: dict[str, Any] | None = None,
    fetched_at: str = "",
) -> StockScorecard:
    """根據各種資料計算四個時間框架的綜合分數與建議。"""
    factor_funcs: dict[str, FactorScore] = {
        "llm_sentiment": factor_llm_sentiment(llm_analysis),
        "logic": factor_logic(logic_result),
        "etf_consensus": factor_etf_consensus(consensus, new_build_signal, add_signal),
        "chips": factor_chips(chip_summary),
        "technical": factor_technical(price, pct_change, volume, technical_snapshot),
        "fundamental": factor_fundamental(fundamental),
        "distribution": factor_distribution(
            distribution_label=distribution_label,
            distribution_detail=distribution_detail,
            distribution_score=distribution_score,
            available=has_distribution,
        ),
        "us_market": factor_us_market(
            macro=macro_snapshot,
            related_us=related_us_stocks,
            adr_premium=adr_premium,
        ),
        "risk": factor_risk(chip_summary, llm_analysis),
    }

    timeframes: dict[str, TimeframeScore] = {}
    for tf, weights in WEIGHTS.items():
        # 拷貝一份帶上 weight 的 factor 清單
        used_factors: list[FactorScore] = []
        weight_sum = 0.0
        available_sum = 0.0
        available_score_sum = 0.0
        for fkey, w in weights.items():
            f = factor_funcs[fkey]
            fc = FactorScore(
                key=f.key, label=f.label, score=f.score,
                weight=w, detail=f.detail, available=f.available,
                sub_scores=dict(f.sub_scores),
            )
            used_factors.append(fc)
            weight_sum += w
            if f.available:
                available_sum += w
                available_score_sum += f.score * w
        # 只用「有資料」的 factor 重新正規化權重；缺資料項不再以中性 50 稀釋總分。
        # 若完全沒有任何資料則維持中性 50。
        if available_sum > 0:
            total = available_score_sum / available_sum
        else:
            total = 50.0

        action, action_label, color = _action_from_score(total)
        confidence = available_sum / weight_sum if weight_sum else 0.0
        notes: list[str] = []
        if confidence < 0.5:
            notes.append("⚠ 資料覆蓋率不足 50%，分數僅基於少數可用因子，請補齊資料再參考")
        elif confidence < 0.8:
            notes.append(f"ℹ 資料覆蓋率 {confidence:.0%}，部分因子缺資料 (已從加權中剔除)")
        if action in ("STRONG_BUY", "BUY") and any(
            f.key == "risk" and f.score < 40 for f in used_factors
        ):
            notes.append("⚠ 風險分偏低，建議降規模或縮停損")
        timeframes[tf] = TimeframeScore(
            timeframe=tf,
            label=TIMEFRAME_LABELS[tf],
            total=round(total, 1),
            action=action,
            action_label=action_label,
            color=color,
            confidence=round(confidence, 3),
            factors=used_factors,
            strategy=_build_strategy(tf, action, price),
            notes=notes,
        )

    summary_notes: list[str] = []
    if all(not f.available for f in factor_funcs.values()):
        summary_notes.append("此股票尚無任何資料；請先跑一次自動化管線")
    elif factor_funcs["llm_sentiment"].available is False:
        summary_notes.append("缺 LLM 法說分析，中長期分數可信度偏低")
    if factor_funcs["chips"].available is False:
        summary_notes.append("缺籌碼面資料，建議先抓近 5 日 TWSE")
    if factor_funcs["us_market"].available is False:
        summary_notes.append("缺美股對照資料，建議跑 `stock-macro-update`")

    return StockScorecard(
        ticker=ticker, name=name, price=price,
        pct_change=pct_change, volume=volume,
        timeframes=timeframes, summary_notes=summary_notes,
        fetched_at=fetched_at,
    )


# ----------------------------------------------------------------------
# 序列化 (給 Streamlit 顯示用)
# ----------------------------------------------------------------------


def scorecard_to_row(s: StockScorecard) -> dict[str, Any]:
    """壓平成一列，方便放進 dataframe。"""
    row: dict[str, Any] = {
        "代號": s.ticker,
        "名稱": s.name,
        "現價": s.price,
        "漲跌(%)": s.pct_change,
        "建議": s.best_action(),
    }
    for tf in TIMEFRAMES:
        score = s.timeframes.get(tf)
        if score:
            label = {
                "day_trade": "當沖", "short_term": "短期",
                "mid_term": "中期", "long_term": "長期",
            }[tf]
            row[f"{label}分"] = score.total
            row[f"{label}建議"] = score.action_label
    return row


__all__ = [
    "ADVISORY_RULE_NOTE",
    "STRATEGY_RULES",
    "TIMEFRAMES",
    "TIMEFRAME_LABELS",
    "WEIGHTS",
    "FactorScore",
    "StockScorecard",
    "TimeframeScore",
    "compute_scorecard",
    "factor_us_market",
    "scorecard_to_row",
]

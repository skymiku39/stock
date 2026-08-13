"""quarterly -- 把 Q1～Q4 財報、營收節奏組合成可分析的時序結構。

對應 Gemini 對話中的「季度紀錄」維度：
* Q1 (5/15 前) -- 作夢行情檢驗
* Q2 (8/14 前) -- 下半年展望定錨
* Q3 (11/14 前) -- 全年獲利定大局
* Q4 (隔年 3/31 前) -- 年報大洗牌

提供：
* `rolling_eps_series()` 算出每個累計節點 (Q1, H1, 9M, FY) 與往年同期比
* `quarter_focus()` 給出該季的市場關注框架
* `revenue_quarterly_aggregate()` 把月營收聚合為季度營收
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from bot.fundamentals_fetcher import MonthlyRevenue, QuarterlyFinancials

# ----------------------------------------------------------------------
# 季度截止/公佈日 (台股實務)
# ----------------------------------------------------------------------

QUARTER_DEADLINES = {
    1: ("Q1 財報", "5/15"),
    2: ("Q2 半年報", "8/14"),
    3: ("Q3 財報", "11/14"),
    4: ("Q4 年報", "隔年 3/31"),
}

QUARTER_FOCUS = {
    1: {
        "title": "作夢行情檢驗",
        "summary": "Q1 是年初題材的檢驗期；需注意春節工作天數對 YoY 的扭曲。",
        "key_questions": [
            "Q1 EPS 是否落在年初市場預估的下緣以上？",
            "1-3 月累計營收 YoY 是否優於去年全年成長率？",
            "毛利率 / 營業利益率是否維持上一季水準？",
        ],
        "what_to_watch": ["題材轉化率", "業績淡季是否守穩", "資本支出年度規劃"],
    },
    2: {
        "title": "下半年展望定錨",
        "summary": "Q2 多屬電子業淡季 (五窮六絕)；市場關注庫存去化與下半年拉貨能見度。",
        "key_questions": [
            "庫存週轉天數有沒有下降？",
            "管理階層對下半年的 guidance 是否上修？",
            "美系/中系客戶 Q3 拉貨力道如何？",
        ],
        "what_to_watch": ["庫存水位", "Capex 是否照表執行", "客戶集中度與訂單能見度"],
    },
    3: {
        "title": "全年獲利定大局",
        "summary": "Q3 是多數產業傳統旺季；前 9 個月 EPS 大局已定，市場開始按計算機推估全年 EPS + 估算明年配息。",
        "key_questions": [
            "9 個月累計 EPS 是否已超越去年全年？",
            "Q4 法說會 guidance 是否暗示淡季不淡？",
            "市場本益比是否還有 Re-rating 空間？",
        ],
        "what_to_watch": [
            "Q4 旺季拉貨持續性", "毛利率三率三升結構", "資產減損 / 匯損潛在風險",
        ],
    },
    4: {
        "title": "年報大洗牌與作帳",
        "summary": "Q4 夾帶全年財報，公佈時間最長；空窗期 (1~3 月) 市場交易股利政策與年度題材。",
        "key_questions": [
            "去年度盈餘分配率是否符合預期？",
            "是否存在大額減損 / 匯損 / 年終獎金提列拖累淨利？",
            "新年度資本支出是上修還是下修？",
        ],
        "what_to_watch": [
            "高股息卡位行情", "營業外損益", "新年度展望與 Capex 結構",
        ],
    },
}


# ----------------------------------------------------------------------
# 模型
# ----------------------------------------------------------------------


@dataclass
class QuarterlyRevenue:
    """季度營收 (從月營收聚合)。"""

    year: int
    quarter: int
    months: list[int] = field(default_factory=list)
    revenue: float = 0.0
    revenue_last_year: float = 0.0
    yoy: float = 0.0


@dataclass
class RollingEpsPoint:
    """滾動 EPS 進度的單點。"""

    label: str           # Q1 / H1 / 9M / FY
    year: int
    quarters: int        # 1-4
    eps_sum: float = 0.0
    prev_year_eps_sum: float = 0.0
    diff_pct: float = 0.0
    note: str = ""


# ----------------------------------------------------------------------
# 聚合
# ----------------------------------------------------------------------


def revenue_quarterly_aggregate(revs: list[MonthlyRevenue]) -> list[QuarterlyRevenue]:
    """把月營收聚合為季度營收 (Q1=1-3, Q2=4-6, Q3=7-9, Q4=10-12)。"""
    if not revs:
        return []
    bucket: dict[tuple, QuarterlyRevenue] = {}
    for r in revs:
        if not r.month or not r.year:
            continue
        q = (r.month - 1) // 3 + 1
        key = (r.year, q)
        qr = bucket.setdefault(key, QuarterlyRevenue(year=r.year, quarter=q))
        qr.months.append(r.month)
        qr.revenue += r.revenue
        qr.revenue_last_year += r.revenue_last_year
    for qr in bucket.values():
        if qr.revenue_last_year > 0:
            qr.yoy = round(100.0 * (qr.revenue - qr.revenue_last_year) / qr.revenue_last_year, 2)
        qr.months = sorted(qr.months)
    return sorted(bucket.values(), key=lambda x: (x.year, x.quarter))


def rolling_eps_series(quarterlies: list[QuarterlyFinancials]) -> list[RollingEpsPoint]:
    """產出每一年度的 Q1/H1/9M/FY 累計 EPS，並與往年同期對比。"""
    if not quarterlies:
        return []
    by_year: dict[int, dict[int, QuarterlyFinancials]] = {}
    for q in quarterlies:
        by_year.setdefault(q.year, {})[q.quarter] = q

    out: list[RollingEpsPoint] = []
    years = sorted(by_year.keys())
    for y in years:
        items = by_year[y]
        for last_q in (1, 2, 3, 4):
            if last_q not in items:
                continue
            label = {1: "Q1", 2: "H1", 3: "9M", 4: "FY"}[last_q]
            cum = sum(items[q].eps for q in range(1, last_q + 1) if q in items)
            prev = by_year.get(y - 1, {})
            prev_cum = sum(prev[q].eps for q in range(1, last_q + 1) if q in prev)
            diff = 0.0
            if abs(prev_cum) > 1e-9:
                diff = round(100.0 * (cum - prev_cum) / abs(prev_cum), 2)
            note = ""
            if last_q == 3 and prev:
                full_prev = sum(prev[q].eps for q in (1, 2, 3, 4) if q in prev)
                if full_prev > 0 and cum > full_prev:
                    note = f"9M 累計 EPS 已超越去年全年 ({cum:.2f} > {full_prev:.2f})"
            out.append(RollingEpsPoint(
                label=label, year=y, quarters=last_q,
                eps_sum=round(cum, 2),
                prev_year_eps_sum=round(prev_cum, 2),
                diff_pct=diff,
                note=note,
            ))
    return out


def quarter_focus(year: int, quarter: int) -> dict[str, Any]:
    """回傳特定年度/季度的市場焦點框架。"""
    base = QUARTER_FOCUS.get(quarter, {}).copy()
    deadline_label, deadline_str = QUARTER_DEADLINES.get(quarter, ("", ""))
    base["year"] = year
    base["quarter"] = quarter
    base["deadline_label"] = deadline_label
    base["deadline_string"] = deadline_str
    return base


def current_quarter_focus(today: dt.date | None = None) -> dict[str, Any]:
    """依今天日期推估目前市場最關注的季度焦點。"""
    today = today or dt.date.today()
    year = today.year
    # 推估「現正在被檢驗」的季度 — 通常是最近一個剛公布的季度
    if today.month <= 5:
        # 1-5 月：去年 Q4 剛公佈 / Q1 即將公佈
        return quarter_focus(year - 1, 4)
    if today.month <= 8:
        return quarter_focus(year, 1)
    if today.month <= 11:
        return quarter_focus(year, 2)
    return quarter_focus(year, 3)


def summarize_quarterly(
    quarterlies: list[QuarterlyFinancials],
    revenues: list[MonthlyRevenue],
    today: dt.date | None = None,
) -> dict[str, Any]:
    """整合所有季度視角，回傳給 UI/scoring 用的 dict。"""
    return {
        "rolling_eps": [_eps_point_to_dict(p) for p in rolling_eps_series(quarterlies)],
        "quarterly_revenue": [
            _qrev_to_dict(qr) for qr in revenue_quarterly_aggregate(revenues)
        ],
        "current_focus": current_quarter_focus(today),
    }


def _eps_point_to_dict(p: RollingEpsPoint) -> dict[str, Any]:
    return {
        "label": p.label,
        "year": p.year,
        "quarters": p.quarters,
        "eps_sum": p.eps_sum,
        "prev_year_eps_sum": p.prev_year_eps_sum,
        "diff_pct": p.diff_pct,
        "note": p.note,
    }


def _qrev_to_dict(qr: QuarterlyRevenue) -> dict[str, Any]:
    return {
        "year": qr.year,
        "quarter": qr.quarter,
        "months": qr.months,
        "revenue": qr.revenue,
        "revenue_last_year": qr.revenue_last_year,
        "yoy": qr.yoy,
    }


__all__ = [
    "QUARTER_DEADLINES",
    "QUARTER_FOCUS",
    "QuarterlyRevenue",
    "RollingEpsPoint",
    "current_quarter_focus",
    "quarter_focus",
    "revenue_quarterly_aggregate",
    "rolling_eps_series",
    "summarize_quarterly",
]

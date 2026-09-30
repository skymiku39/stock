"""可追蹤的 ``!ask`` 查詢規劃契約。

這裡不使用「快速分類失敗就結束」的分支。每個問題都會產生一份
``QueryPlan``；若規則無法可靠判斷領域，只會標記 ``planner_required``，並
保留安全的 fallback 資源，讓後續可以交給 LLM planner 或人工檢查。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional


RESOURCE_ORDER = (
    "memory",
    "historical_evidence",
    "channel_knowledge",
    "user_memory",
    "ticker_snapshot",
    "technical",
    "fundamental",
    "chips",
    "etf",
    "taifex",
    "macro",
    "news",
    "mops",
    "web_search",
    "professional_knowledge",
)
VALID_RESOURCES = set(RESOURCE_ORDER)

TICKER_RE = re.compile(r"(?<![A-Za-z0-9])([0-9]{4,6}[A-Za-z]?)(?![A-Za-z0-9])", re.I)
TOKEN_RE = re.compile(r"[0-9]{4,6}[A-Za-z]?|[A-Za-z][A-Za-z0-9_.-]{1,}|[\u3400-\u9fff]{2,}")

_RESOURCE_RULES = (
    ("historical_evidence", ("歷史", "過去", "以前", "曾經", "說過", "原句", "哪天", "幾月幾日")),
    ("channel_knowledge", ("頻道", "主播", "台規", "規則", "行程", "社群")),
    ("user_memory", ("我喜歡", "我的偏好", "稱呼我", "記得我", "我的習慣", "我曾經")),
    ("technical", ("技術", "均線", "RSI", "MACD", "KD", "布林", "K線", "量價")),
    ("fundamental", (
        "基本面", "營收", "財報", "EPS", "毛利", "淨利", "估值", "本益比",
        "股利", "股息", "配息", "殖利率", "除息", "除權",
    )),
    ("chips", ("籌碼", "外資", "投信", "自營商", "融資", "融券", "借券")),
    ("etf", ("ETF", "持股", "成分股", "新建倉", "加碼")),
    ("taifex", ("台指期", "台指期貨", "期貨", "正逆價差", "基差", "未平倉", "TAIFEX")),
    ("macro", ("總經", "美股", "費半", "NASDAQ", "VIX", "匯率", "利率")),
    # 「最新／今天／即時」只代表 freshness，不等於使用者一定要查新聞；
    # 例如「最新營收」應使用 fundamental，而不是被誤分到 news。
    ("news", (
        "新聞", "消息", "公告", "重大訊息", "重大公告", "新聞稿", "快訊",
        "事件", "市場消息", "法說", "法說會", "法人說明會", "公開資訊觀測站",
        "MOPS", "mops",
    )),
    ("professional_knowledge", ("專業", "研究", "官方文件", "規範", "論文", "專家")),
)

_FORECAST_WORDS = ("預測", "展望", "未來", "可能", "趨勢", "前瞻")
_FRESH_WORDS = ("最新", "今天", "即時", "剛剛", "目前")
_RECENT_WORDS = ("最近", "近期", "近來")
_TEMPORAL_STRICT_WORDS = (
    "同日", "同一天", "同時點", "同一時點", "同步", "一致時點", "同一交易日",
)
VALID_TEMPORAL_POLICIES = {"allow_mixed", "warn_mixed", "same_date"}

# Search engines work better with a short semantic query than with the full
# conversational instruction.  These are only query-construction stop words;
# they do not affect classification or the original query sent to the LLM.
_SEARCH_NOISE_TERMS = (
    "請分別標示", "分別標示", "標示", "資料日期", "抓取時間", "來源",
    "不確定性", "不確定", "背景", "請問", "請幫我", "幫我", "一下",
    "最近", "近期", "近來", "最新", "今天", "目前", "剛剛", "即時",
    "以及", "還有", "與", "和", "及", "的", "是", "嗎", "呢",
)
_ALL_RESOURCE_TERMS = tuple(
    term for _resource, terms in _RESOURCE_RULES for term in terms
)


@dataclass(frozen=True)
class QueryPlan:
    """一次問題拆解後的可序列化計畫。"""

    plan_id: str
    trace_id: str
    query: str
    normalized_query: str
    keywords: List[str] = field(default_factory=list)
    resource_needs: List[str] = field(default_factory=list)
    ticker: str = ""
    scope_type: str = "global"
    scope_id: str = ""
    freshness: str = "as_available"
    temporal_policy: str = "warn_mixed"
    citation_required: bool = True
    allow_forecast: bool = False
    max_sources: int = 8
    planner_required: bool = False
    planner_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _unique(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _coerce_bool(value: Any, fallback: bool) -> bool:
    """只接受明確的布林表示，避免 bool("false") 變成 True。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return fallback


def _extract_keywords(query: str, ticker: str) -> List[str]:
    tokens = [match.group(0).strip() for match in TOKEN_RE.finditer(query)]
    # CJK tokenizer 往往會把「最新營收」「頻道以前」視為一整段；把已命中
    # 的註冊詞一併放入搜尋詞，避免只用整句造成 FTS/LIKE 零召回。
    matched_terms = [
        term
        for _resource, terms in _RESOURCE_RULES
        for term in terms
        if term.lower() in query.lower()
    ]
    return _unique([ticker, *matched_terms, *tokens])


def _clean_search_piece(value: Any) -> List[str]:
    """Extract compact search tokens without rewriting the user's query."""
    text = str(value or "").strip()
    if not text:
        return []
    # Remove registered resource words here; they are re-added as individual
    # terms below.  This turns ``台積電最近的營收`` into ``台積電`` rather than
    # sending a long CJK phrase that many public endpoints handle poorly.
    for term in sorted((*_ALL_RESOURCE_TERMS, *_SEARCH_NOISE_TERMS), key=len, reverse=True):
        text = text.replace(term, " ")
    text = re.sub(r"[，。！？、；：:;,!?（）()【】\[\]「」『』\"'／/\\|]", " ", text)
    return [
        token
        for match in TOKEN_RE.finditer(text)
        for token in [match.group(0).strip()]
        if len(token) >= 2
    ]


def build_search_query(plan: "QueryPlan", *, resource: str = "") -> str:
    """Build a bounded query for web/news adapters.

    ``plan.query`` remains the authoritative user question for the LLM and
    audit trail.  External search receives only the ticker, registered intent
    terms, and compact free-form tokens, so formatting instructions do not
    become search terms and accidentally suppress useful results.
    """
    original = plan.normalized_query or plan.query
    matched_terms = [
        term
        for _resource, terms in _RESOURCE_RULES
        for term in terms
        if term.lower() in original.lower()
    ]
    free_tokens = _clean_search_piece(original)
    planner_free_tokens = [
        term if term in _ALL_RESOURCE_TERMS else piece
        for term in plan.keywords
        for piece in ([term] if term in _ALL_RESOURCE_TERMS else _clean_search_piece(term))
    ]

    # Search providers generally interpret whitespace as AND.  A query that
    # asks for revenue, valuation, dividends, macro, news, and a presentation
    # at once therefore becomes over-constrained.  News gets a small OR group
    # of its actual news intent plus at most three topical terms; the other
    # resources retain the compact AND query.
    if resource == "news":
        news_terms = [
            term for _resource, terms in _RESOURCE_RULES if _resource == "news"
            for term in terms if term.lower() in original.lower()
        ]
        topical_terms = [
            term for term in matched_terms if term not in news_terms
        ][:3]
        intent_terms = _unique([*news_terms[:3], *topical_terms[:1]])
        planner_subject_tokens = [
            token for token in planner_free_tokens
            if token not in _ALL_RESOURCE_TERMS
        ]
        candidates = _unique([
            plan.ticker, *free_tokens, *planner_subject_tokens,
        ])
        if intent_terms:
            query = " ".join(candidates[:4]).strip()
            query = f"{query} ({' OR '.join(intent_terms[:6])})".strip()
        else:
            query = " ".join(candidates[:8]).strip()
    else:
        candidates = [plan.ticker, *matched_terms, *free_tokens, *planner_free_tokens]
        compact = _unique(candidates)
        query = " ".join(compact[:12]).strip()
    # A public search endpoint should never receive the whole conversational
    # prompt.  Keep enough terms for multi-resource questions while bounding
    # latency, cache-key churn, and provider-specific query length limits.
    if resource == "news":
        freshness_hint = {
            "24h": "when:1d",
            "7d": "when:7d",
            "30d": "when:30d",
        }.get(str(plan.freshness or "").lower())
        if freshness_hint:
            query = f"{query} {freshness_hint}".strip()
    return query or (plan.ticker or original[:120])


def _resource_needs(query: str, ticker: str) -> tuple[List[str], bool, str]:
    needs: List[str] = ["memory"]
    matched = False
    for resource, terms in _RESOURCE_RULES:
        if any(term.lower() in query.lower() for term in terms):
            needs.append(resource)
            matched = True

    if ticker:
        needs.append("ticker_snapshot")
        # MOPS is a concrete official source used alongside news for a
        # ticker-specific announcement query.  Keep it in the serialised plan
        # instead of adding it later as an implicit fallback, so provenance,
        # missing-resource status, and planner output all describe the same
        # work that will actually be performed.
        if "news" in needs:
            needs.append("mops")
    if not matched:
        # 未知問題仍保留可供 planner 接手的安全 fallback；沒有 "other"
        # 分支會把問題吞掉的情況。
        needs.extend(("historical_evidence", "news", "web_search"))
        return _unique(needs), True, "未命中特定領域，需由 LLM planner 補充資源判斷"
    return _unique(needs), False, "規則命中一個或多個資料領域"


def plan_query(
    query: str,
    *,
    ticker: str = "",
    scope_type: str = "global",
    scope_id: str = "",
    trace_id: str = "",
    max_sources: int = 8,
) -> QueryPlan:
    """為每個問題建立計畫；不會因分類不確定而拒絕建立計畫。"""
    original = str(query or "").strip()
    if not original:
        raise ValueError("query 不可為空")
    normalized = re.sub(r"\s+", " ", original)
    detected = (ticker or "").strip().upper()
    if not detected:
        match = TICKER_RE.search(normalized)
        detected = match.group(1).upper() if match else ""
    keywords = _extract_keywords(normalized, detected)
    needs, planner_required, planner_reason = _resource_needs(normalized, detected)
    if any(word in normalized for word in _FRESH_WORDS):
        freshness = "24h"
    elif any(word in normalized for word in _RECENT_WORDS):
        # 「最近」通常不是嚴格的即時 24 小時，而是可審核的短期窗口。
        freshness = "7d"
    else:
        freshness = "as_available"
    if any(word in normalized for word in ("歷史", "過去", "以前", "曾經", "原句")):
        freshness = "unbounded"
    temporal_policy = (
        "same_date"
        if any(word in normalized for word in _TEMPORAL_STRICT_WORDS)
        else "warn_mixed"
    )
    allow_forecast = any(word in normalized for word in _FORECAST_WORDS)
    digest = hashlib.sha256(
        "|".join((normalized, detected, scope_type, scope_id)).encode("utf-8")
    ).hexdigest()[:20]
    return QueryPlan(
        plan_id=f"plan:{digest}",
        trace_id=trace_id or f"trace:{uuid.uuid4().hex}",
        query=original,
        normalized_query=normalized,
        keywords=keywords,
        resource_needs=needs,
        ticker=detected,
        scope_type=str(scope_type or "global"),
        scope_id=str(scope_id or ""),
        freshness=freshness,
        temporal_policy=temporal_policy,
        citation_required=True,
        allow_forecast=allow_forecast,
        max_sources=max(1, min(50, int(max_sources))),
        planner_required=planner_required,
        planner_reason=planner_reason,
    )


def normalize_planner_payload(
    payload: Any,
    *,
    fallback: QueryPlan,
) -> QueryPlan:
    """將未來 LLM planner 的 JSON 限制在已知契約內。

    未知 resource 會被丟棄，空值回到 deterministic fallback；LLM 不能藉此
    任意注入新的工具名稱或取消引用要求。
    """
    obj = payload if isinstance(payload, Mapping) else {}
    raw_resources = obj.get("resource_needs")
    resources = [
        str(value).strip().lower()
        for value in (raw_resources if isinstance(raw_resources, list) else [])
        if str(value).strip().lower() in VALID_RESOURCES
    ]
    # Deterministic planning is a safety boundary.  For a query that matched
    # explicit user wording, the LLM may discover additional resources but it
    # must not remove an inferred requirement (for example ``新聞``).  For an
    # unknown query, however, the deterministic list is only a safe fallback;
    # keeping all of it after a successful planner would make every ambiguous
    # question require news/web even when the planner selected a different
    # valid domain.
    has_valid_resource_list = isinstance(raw_resources, list) and bool(resources)
    if fallback.planner_required:
        resources = resources if has_valid_resource_list else list(fallback.resource_needs)
    else:
        resources = _unique([*fallback.resource_needs, *resources])
    resources = _unique(["memory", *resources])
    raw_keywords = obj.get("keywords")
    keywords = _unique(
        [*fallback.keywords,
         *([str(value) for value in raw_keywords if str(value).strip()]
           if isinstance(raw_keywords, list) else [])]
    )
    # An LLM planner cannot relax a freshness constraint derived from the
    # user's wording.  It may make an otherwise unconstrained query stricter,
    # but "最新" must not become ``as_available``.
    candidate_freshness = str(obj.get("freshness") or fallback.freshness).strip().lower()
    if candidate_freshness not in {"24h", "7d", "30d", "as_available", "unbounded"}:
        candidate_freshness = fallback.freshness
    freshness = (
        fallback.freshness
        if fallback.freshness in {"24h", "7d", "30d", "unbounded"}
        else candidate_freshness
    )
    candidate_temporal = str(
        obj.get("temporal_policy") or fallback.temporal_policy
    ).strip().lower()
    if candidate_temporal not in VALID_TEMPORAL_POLICIES:
        candidate_temporal = fallback.temporal_policy
    temporal_policy = (
        fallback.temporal_policy
        if fallback.temporal_policy == "same_date"
        else candidate_temporal
    )
    try:
        max_sources = int(obj.get("max_sources", fallback.max_sources) or fallback.max_sources)
    except (TypeError, ValueError):
        max_sources = fallback.max_sources
    return QueryPlan(
        **{
            **fallback.to_dict(),
            "keywords": keywords,
            "resource_needs": resources,
            "freshness": freshness,
            "temporal_policy": temporal_policy,
            # 引用是系統安全政策，不接受 LLM planner 關閉。
            "citation_required": True,
            # Forecast access is also policy-bound: a planner cannot turn a
            # factual question into a forecast question.
            "allow_forecast": (
                fallback.allow_forecast
                and _coerce_bool(obj.get("allow_forecast"), True)
            ),
            "max_sources": max(1, min(50, max_sources)),
            "planner_required": False,
            "planner_reason": "LLM planner 已完成規劃",
        }
    )


__all__ = [
    "QueryPlan",
    "RESOURCE_ORDER",
    "VALID_RESOURCES",
    "VALID_TEMPORAL_POLICIES",
    "normalize_planner_payload",
    "build_search_query",
    "plan_query",
]

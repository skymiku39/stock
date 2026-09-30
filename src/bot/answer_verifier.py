"""``!ask`` 回答的來源連結驗證。

這個模組不宣稱能證明 LLM 的語意必然正確；它先做可自動且可稽核的最低
門檻：回答必須引用 EvidencePack 內存在、且品質可用的 evidence id，claims
若有提供也必須逐項列出支援來源。未通過就 abstain。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Set

from bot.evidence import EvidencePack, is_conversation_evidence


@dataclass
class VerificationResult:
    valid: bool
    cited_evidence_ids: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    claim_support: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    numeric_checks: List[Dict[str, Any]] = field(default_factory=list)
    method: str = "evidence_id_linkage+lexical_claim_support+numeric_date_checks"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "cited_evidence_ids": list(self.cited_evidence_ids),
            "errors": list(self.errors),
            "claim_support": list(self.claim_support),
            "conflicts": list(self.conflicts),
            "numeric_checks": list(self.numeric_checks),
            "method": self.method,
        }


def _as_ids(value: Any) -> List[str]:
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    result: List[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)\s*%?")
_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,}")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]{2,}")
_PUNCT_RE = re.compile(r"[^0-9A-Za-z\u3400-\u9fff%+\-.]+")
_DATE_RE = re.compile(
    r"(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{3,4}年\d{1,2}月\d{1,2}日)"
)
_MEASUREMENT_UNITS = (
    "%", "倍", "元", "億", "兆", "萬", "万", "千", "百", "張", "股",
    "筆", "檔", "家", "點", "美元", "美金", "台幣", "新台幣",
)


def _match_text(value: Any) -> str:
    """建立適合保守比對的文字；不做同義詞推論。"""
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return _PUNCT_RE.sub("", text)


def _claim_anchors(value: Any) -> List[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    anchors: List[str] = []
    anchors.extend(token.replace(" ", "") for token in _NUMBER_RE.findall(text))
    anchors.extend(_ASCII_WORD_RE.findall(text))
    for run in _CJK_RUN_RE.findall(text):
        # 以二字片段支援中文句子略有措辭差異，但不使用模型猜同義詞。
        anchors.extend(run[index:index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(anchor for anchor in anchors if anchor))


def _answer_measurements(value: Any) -> tuple[List[str], List[str]]:
    """取回答正文中的可核對數值與完整日期。

    只抓帶單位、百分比或小數的數字；四位數 ticker／年份不在這裡當成
    財務數值，避免把「2330」或「2026」誤判成未被證據支持的金額。
    """
    text = unicodedata.normalize("NFKC", str(value or "")).replace(",", "")
    numbers: List[str] = []
    for match in _NUMBER_RE.finditer(text):
        token = match.group(0).replace(" ", "")
        suffix = text[match.end():match.end() + 8]
        if "%" in token or "." in token or any(
            suffix.startswith(unit) for unit in _MEASUREMENT_UNITS
        ):
            if token not in numbers:
                numbers.append(token)
    dates = list(dict.fromkeys(
        value.replace("/", "-") for value in _DATE_RE.findall(text)
    ))
    return numbers, dates


def _claim_support(
    statement: str,
    evidence_items: Iterable[Mapping[str, Any]],
) -> tuple[float, List[str]]:
    """回傳最佳文字支持分數與匹配的 evidence IDs。

    這不是 NLI 或事實真偽證明，而是回答送出前的低成本保守閘門：數字若出現
    於主張中必須在來源片段中出現；文字主張至少要有足夠的原文片段重疊。
    """
    claim_text = _match_text(statement)
    if not claim_text:
        return 0.0, []
    claim_numbers = [token.replace(" ", "") for token in _NUMBER_RE.findall(statement)]
    anchors = _claim_anchors(statement)
    best_score = 0.0
    matched_ids: List[str] = []
    for item in evidence_items:
        evidence_id = str(item.get("id") or "")
        evidence_text = _match_text(
            " ".join((str(item.get("title") or ""), str(item.get("excerpt") or "")))
        )
        if not evidence_text:
            continue
        if claim_text in evidence_text:
            score = 1.0
        else:
            if claim_numbers and not all(number in evidence_text for number in claim_numbers):
                continue
            overlap = sum(anchor in evidence_text for anchor in anchors)
            if not anchors or overlap < max(2, math.ceil(len(anchors) * 0.5)):
                continue
            score = overlap / len(anchors)
        if score > best_score:
            best_score = score
            matched_ids = [evidence_id]
        elif score == best_score and score > 0 and evidence_id not in matched_ids:
            matched_ids.append(evidence_id)
    return best_score, matched_ids


def _claim_conflicts(
    statement: str,
    evidence_items: Iterable[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """找出同一個文字主題下互斥的數字；不做語意推測。"""
    anchors = set(_claim_anchors(statement))
    rows = list(evidence_items)
    values: Dict[str, List[str]] = {}
    for item in rows:
        text = str(item.get("title") or "") + " " + str(item.get("excerpt") or "")
        if anchors and not any(anchor in _match_text(text) for anchor in anchors if not _NUMBER_RE.fullmatch(anchor)):
            continue
        nums = []
        for value in _NUMBER_RE.findall(text):
            normalized = value.replace(" ", "")
            # Ticker/year identifiers are not competing measurements.
            bare = normalized.rstrip("%")
            try:
                if "%" not in normalized and float(bare) >= 1000:
                    continue
            except ValueError:
                pass
            nums.append(normalized)
        for number in nums:
            values.setdefault(number, []).append(str(item.get("id") or ""))
    if len(values) <= 1 or len(rows) < 2:
        return []
    # A single source can legitimately contain several numbers (ticker,
    # period and metric).  Only report a conflict when different source items
    # contain different candidate measurements.
    source_sets: List[set[str]] = []
    for item in rows:
        text = str(item.get("title") or "") + " " + str(item.get("excerpt") or "")
        source_sets.append({
            value.replace(" ", "") for value in _NUMBER_RE.findall(text)
            if "%" in value or (value.replace(" ", "").isdigit() and int(value.replace(" ", "")) < 1000)
        })
    competing = any(
        left and right and left.isdisjoint(right)
        for index, left in enumerate(source_sets)
        for right in source_sets[index + 1:]
    )
    if not competing:
        return []
    return [{"values": list(values), "evidence_ids": list(dict.fromkeys(
        eid for ids in values.values() for eid in ids if eid
    ))}]


def verify_answer_payload(
    payload: Any,
    evidence_pack: EvidencePack,
) -> VerificationResult:
    """驗證結構化回答是否只引用本次 EvidencePack 的可用證據。"""
    errors: List[str] = []
    obj = payload if isinstance(payload, Mapping) else {}
    if not isinstance(payload, Mapping):
        errors.append("answer payload 不是 JSON object")
    answer = str(obj.get("answer") or "").strip()
    if not answer:
        errors.append("answer 為空")
    is_uncertain_answer = answer.startswith("我不確定")

    confidence = obj.get("confidence")
    if isinstance(confidence, bool):
        errors.append("confidence 必須是 0 到 1 的數字")
    else:
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = None
        if confidence_value is None or not math.isfinite(confidence_value):
            errors.append("confidence 必須是有限數字")
        elif not 0.0 <= confidence_value <= 1.0:
            errors.append("confidence 必須介於 0 與 1")

    usable_ids: Set[str] = {
        str(item.get("id")) for item in evidence_pack.usable_items
    }
    usable_by_id = {
        str(item.get("id")): item for item in evidence_pack.usable_items
    }
    cited = _as_ids(obj.get("evidence_ids"))
    if not cited:
        errors.append("回答沒有引用 evidence_ids")
    unknown = [item for item in cited if item not in usable_ids]
    if unknown:
        errors.append("引用不存在或品質不可用的 evidence: " + ", ".join(unknown))

    claim_support: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    numeric_checks: List[Dict[str, Any]] = []
    cited_items = [
        usable_by_id[evidence_id]
        for evidence_id in cited
        if evidence_id in usable_by_id
    ]
    if not is_uncertain_answer and cited_items:
        answer_numbers, answer_dates = _answer_measurements(answer)
        cited_text = _match_text(" ".join(
            str(item.get("title") or "") + " " + str(item.get("excerpt") or "")
            for item in cited_items
        ))
        missing_answer_numbers = [
            value for value in answer_numbers if _match_text(value) not in cited_text
        ]
        missing_answer_dates = [
            value for value in answer_dates if _match_text(value) not in cited_text
        ]
        numeric_checks.append({
            "scope": "answer",
            "numbers": answer_numbers,
            "dates": answer_dates,
            "missing_numbers": missing_answer_numbers,
            "missing_dates": missing_answer_dates,
        })
        if missing_answer_numbers:
            errors.append("回答正文的數值未出現在 cited evidence 原文中")
        if missing_answer_dates:
            errors.append("回答正文的日期未出現在 cited evidence 原文中")
    claims = obj.get("claims")
    if claims is None:
        if not is_uncertain_answer:
            errors.append("可回答的結果必須列出 claims")
    else:
        if not isinstance(claims, list):
            errors.append("claims 必須是陣列")
        else:
            if not claims and not is_uncertain_answer:
                errors.append("可回答的結果 claims 不可為空")
            for index, claim in enumerate(claims):
                if not isinstance(claim, Mapping):
                    errors.append(f"claims[{index}] 必須是 object")
                    continue
                statement = str(claim.get("text") or claim.get("statement") or "").strip()
                claim_ids = _as_ids(claim.get("evidence_ids"))
                if not statement:
                    errors.append(f"claims[{index}] 缺少文字")
                if not claim_ids:
                    errors.append(f"claims[{index}] 沒有 evidence_ids")
                for evidence_id in claim_ids:
                    if evidence_id not in usable_ids:
                        errors.append(
                            f"claims[{index}] 引用不存在或品質不可用的 evidence: {evidence_id}"
                        )
                for evidence_id in claim_ids:
                    if evidence_id not in cited:
                        errors.append(
                            f"claims[{index}] 的 evidence 未列在回答 evidence_ids: {evidence_id}"
                        )
                if claim_ids:
                    linked_items = [usable_by_id[evidence_id] for evidence_id in claim_ids
                                    if evidence_id in usable_by_id]
                    score, matched_ids = _claim_support(
                        statement,
                        linked_items,
                    )
                    claim_support.append({
                        "index": index,
                        "supported": bool(matched_ids),
                        "score": round(score, 3),
                        "matched_evidence_ids": matched_ids,
                        "source_kinds": sorted({str(item.get("kind") or "unknown") for item in linked_items}),
                        "sources": sorted({str(item.get("source") or "unknown") for item in linked_items}),
                    })
                    if not matched_ids:
                        errors.append(
                            f"claims[{index}] 的文字無法在列出的 evidence 原文中核對"
                        )
                    claim_numbers = [v.replace(" ", "") for v in _NUMBER_RE.findall(statement)]
                    claim_dates = [v.replace("/", "-") for v in _DATE_RE.findall(statement)]
                    linked_text = _match_text(" ".join(
                        str(item.get("title") or "") + " " + str(item.get("excerpt") or "")
                        for item in linked_items
                    ))
                    missing_numbers = [v for v in claim_numbers if v not in linked_text]
                    missing_dates = [v for v in claim_dates if _match_text(v) not in linked_text]
                    numeric_checks.append({
                        "index": index,
                        "numbers": claim_numbers,
                        "dates": claim_dates,
                        "missing_numbers": missing_numbers,
                        "missing_dates": missing_dates,
                    })
                    if missing_numbers:
                        errors.append(f"claims[{index}] 的數值未出現在 evidence 原文中")
                    if missing_dates:
                        errors.append(f"claims[{index}] 的日期未出現在 evidence 原文中")
                    claim_type = str(claim.get("type") or claim.get("claim_type") or "fact").lower()
                    uncertainty = str(claim.get("uncertainty") or "").strip()
                    source_names = {
                        str(item.get("source") or "").lower() for item in linked_items
                    }
                    source_kinds = {
                        str(item.get("kind") or "").lower() for item in linked_items
                    }
                    official = any(
                        value in {"official", "twse", "tpex", "mops", "tdcc", "taifex"}
                        or "official" in value
                        for value in source_names
                    )
                    if "官方" in statement and not official:
                        errors.append(f"claims[{index}] 宣稱官方來源，但引用來源未標示官方")
                    if claim_type == "fact" and source_kinds.intersection({"forecast", "prediction"}) and not uncertainty:
                        errors.append(f"claims[{index}] 不可把預測來源表述為既定事實")
                    conversation_cited = [
                        str(item.get("id") or "")
                        for item in linked_items
                        if is_conversation_evidence(item)
                    ]
                    if claim_type in {"fact", ""} and conversation_cited:
                        errors.append(
                            f"claims[{index}] 不可引用對話記憶作為市場／官方事實證據: "
                            + ", ".join(conversation_cited)
                        )
                    if claim_type in {"forecast", "prediction", "estimate"} and not any(
                        marker in statement for marker in ("預測", "可能", "估計", "推測", "假設")
                    ):
                        errors.append(f"claims[{index}] 的預測內容未標示不確定性")
                    claim_conflicts = _claim_conflicts(statement, linked_items)
                    if claim_conflicts:
                        conflicts.extend({"index": index, **value} for value in claim_conflicts)
                        if not uncertainty and not is_uncertain_answer:
                            errors.append(f"claims[{index}] 的來源存在未解決衝突")

    return VerificationResult(
        valid=not errors,
        cited_evidence_ids=cited,
        errors=errors,
        claim_support=claim_support,
        conflicts=conflicts,
        numeric_checks=numeric_checks,
    )


__all__ = ["VerificationResult", "verify_answer_payload"]

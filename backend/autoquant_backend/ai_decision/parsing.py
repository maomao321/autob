from __future__ import annotations

import json
import math
from typing import Any

from autoquant_backend.ai_decision.models import (
    DecisionError,
    EntryTimingDecision,
    OpeningDecision,
)
from autoquant_backend.ai_decision.sanitizing import (
    _clean_text_list,
    _clean_text_value,
)
from autoquant_shared.models import Direction


def parse_opening_decision(
    content: str, provider: str, model: str
) -> OpeningDecision:
    if not content or not content.strip():
        raise DecisionError(f"{provider} 返回空响应")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DecisionError(f"{provider} 未返回合法 JSON") from exc
    if not isinstance(payload, dict):
        raise DecisionError(f"{provider} 决策不是 JSON 对象")
    required = {"direction", "confidence", "summary", "factors", "risks"}
    if set(payload) != required:
        raise DecisionError(f"{provider} 决策字段不符合约定")

    direction_raw = payload["direction"]
    if direction_raw not in {"LONG", "SHORT", "FLAT"}:
        raise DecisionError(f"{provider} 返回了未知方向")
    confidence_raw = payload["confidence"]
    if isinstance(confidence_raw, bool) or not isinstance(
        confidence_raw, (int, float)
    ):
        raise DecisionError(f"{provider} 置信度格式错误")
    confidence = float(confidence_raw)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise DecisionError(f"{provider} 置信度超出范围")

    summary = _clean_text_value(payload["summary"], "summary", 500, provider)
    factors = _clean_text_list(payload["factors"], "factors", 6, provider)
    risks = _clean_text_list(payload["risks"], "risks", 5, provider)
    return OpeningDecision(
        direction=Direction(direction_raw),
        confidence=confidence,
        summary=summary,
        factors=factors,
        risks=risks,
        provider=provider,
        model=model,
    )


def parse_entry_timing_decision(
    content: str, provider: str, model: str
) -> EntryTimingDecision:
    if not content or not content.strip():
        raise DecisionError(f"{provider} 返回空响应")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DecisionError(f"{provider} 未返回合法 JSON") from exc
    if not isinstance(payload, dict):
        raise DecisionError(f"{provider} 时机决策不是 JSON 对象")
    required = {"enter_now", "confidence", "summary", "factors", "risks"}
    if set(payload) != required:
        raise DecisionError(f"{provider} 时机决策字段不符合约定")

    enter_now = payload["enter_now"]
    if not isinstance(enter_now, bool):
        raise DecisionError(f"{provider} 的 enter_now 格式错误")
    confidence_raw = payload["confidence"]
    if isinstance(confidence_raw, bool) or not isinstance(
        confidence_raw, (int, float)
    ):
        raise DecisionError(f"{provider} 置信度格式错误")
    confidence = float(confidence_raw)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise DecisionError(f"{provider} 置信度超出范围")

    summary = _clean_text_value(payload["summary"], "summary", 500, provider)
    factors = _clean_text_list(payload["factors"], "factors", 6, provider)
    risks = _clean_text_list(payload["risks"], "risks", 5, provider)
    return EntryTimingDecision(
        enter_now=enter_now,
        confidence=confidence,
        summary=summary,
        factors=factors,
        risks=risks,
        provider=provider,
        model=model,
    )



from __future__ import annotations

import html
import math
from decimal import Decimal
from typing import Any

from autoquant_shared.formatting import financial_text


def _safe_structured_context(value: Any) -> dict[str, Any]:
    """Copy bounded JSON-compatible strategy metadata into a model input."""

    def convert(item: Any, depth: int) -> Any:
        if depth > 6:
            return None
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            for raw_key, raw_value in list(item.items())[:60]:
                key = _clean_text(str(raw_key), 80)
                if key:
                    result[key] = convert(raw_value, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            return [convert(child, depth + 1) for child in item[:100]]
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else None
        if isinstance(item, Decimal):
            return financial_text(item) if item.is_finite() else None
        return _clean_text(str(item), 1200)

    converted = convert(value, 0)
    return converted if isinstance(converted, dict) else {}


def _clean_text_list(
    value: Any, field_name: str, limit: int, provider: str
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise DecisionError(f"{provider} 的 {field_name} 格式错误")
    result: list[str] = []
    for item in value:
        result.append(_clean_text_value(item, field_name, 240, provider))
    return tuple(result)


def _clean_text_value(
    value: Any, field_name: str, max_length: int, provider: str
) -> str:
    if not isinstance(value, str):
        raise DecisionError(f"{provider} 的 {field_name} 格式错误")
    result = _clean_text(value, max_length)
    if not result:
        raise DecisionError(f"{provider} 的 {field_name} 不能为空")
    return result


def _clean_text(value: str, max_length: int) -> str:
    return " ".join(html.unescape(str(value)).split())[:max_length]


def _safe_error(exc: Exception) -> str:
    message = _clean_text(str(exc), 240)
    return message or exc.__class__.__name__

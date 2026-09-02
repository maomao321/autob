from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from autoquant_shared.formatting import financial_text
from autoquant_shared.models import AccountOverview, RuntimeSnapshot


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def snapshot_payload(snapshot: RuntimeSnapshot) -> dict[str, Any]:
    payload = _json_value(asdict(snapshot))
    for field_name in (
        "last_price",
        "ma_value",
        "average_entry_price",
        "session_open_notional",
        "realized_pnl",
        "unrealized_pnl",
        "profit",
    ):
        value = getattr(snapshot, field_name)
        payload[field_name] = None if value is None else financial_text(value)
    return payload


def overview_payload(overview: AccountOverview) -> dict[str, Any]:
    payload = _json_value(asdict(overview))
    for field_name in ("total_balance", "realized_pnl", "unrealized_pnl"):
        value = getattr(overview, field_name)
        payload[field_name] = None if value is None else financial_text(value)
    return payload



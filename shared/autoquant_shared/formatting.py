from __future__ import annotations

from decimal import Decimal


def financial_text(value: Decimal) -> str:
    """Format a monetary amount or price with exactly two decimal places."""
    return format(value, ".2f")

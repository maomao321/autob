from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from autoquant_shared.config import default_config_path


CONSUMED_STATUSES = {
    "SUBMITTING",
    "UNKNOWN",
    "ACKNOWLEDGED",
    "NEW",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELED",
    "EXPIRED",
}
RECONCILABLE_STATUSES = {
    "ACKNOWLEDGED",
    "NEW",
    "ACCEPTED",
    "PARTIALLY_FILLED",
}


def default_state_path() -> Path:
    return default_config_path().with_name("orders.sqlite3")


@dataclass(frozen=True, slots=True)
class OrderRecord:
    client_order_id: str
    symbol: str
    side: str
    trading_day: int
    status: str
    order_id: str
    paper: bool
    created_at: int
    updated_at: int
    message: str
    requested_notional: Decimal
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal
    fee: Decimal
    realized_pnl: Decimal
    reduce_only: bool


@dataclass(frozen=True, slots=True)
class PositionSummary:
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    open_fee: Decimal = Decimal("0")
    additions: int = 0


@dataclass(frozen=True, slots=True)
class PortfolioPerformance:
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal | None = Decimal("0")
    missing_price_symbols: tuple[str, ...] = ()


class RiskLimitError(RuntimeError):
    """An atomic account-level risk reservation was rejected."""



from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"
    UNKNOWN = "UNKNOWN"


class RunState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    WARMING_UP = "WARMING_UP"
    RUNNING = "RUNNING"
    SIGNAL = "SIGNAL"
    ERROR = "ERROR"
    STOPPING = "STOPPING"


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")
    closed: bool = False
    event_time: int = 0


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    side: Side
    price: Decimal
    ma_value: Decimal
    bar_open_time: int
    reason: str


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: Side
    reference_price: Decimal
    buy_notional: Decimal
    sell_quantity: Decimal
    client_order_id: str = ""
    reduce_only: bool = False
    allow_short: bool = False


@dataclass(frozen=True, slots=True)
class OrderResult:
    accepted: bool
    order_id: str
    message: str
    paper: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RuntimeSnapshot:
    symbol: str
    state: RunState = RunState.STOPPED
    direction: Direction = Direction.UNKNOWN
    last_price: Decimal | None = None
    ma_value: Decimal | None = None
    warmup_bars: int = 0
    warmup_required: int = 0
    trades_today: int = 0
    position_quantity: Decimal = Decimal("0")
    average_entry_price: Decimal = Decimal("0")
    pending_orders: int = 0
    session_open_notional: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal | None = Decimal("0")
    profit: Decimal | None = Decimal("0")
    message: str = "未启动"
    updated_at: int = 0


@dataclass(frozen=True, slots=True)
class AccountOverview:
    total_balance: Decimal | None = None
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal | None = None
    currency: str = "USDC"
    missing_price_symbols: tuple[str, ...] = ()
    message: str = "尚未刷新"
    updated_at: int = 0


@dataclass(frozen=True, slots=True)
class TradeHistoryItem:
    executed_at: int
    symbol: str
    action: str
    opening_direction: str
    price: Decimal
    quantity: Decimal
    amount: Decimal
    fee: Decimal
    profit: Decimal
    paper: bool


@dataclass(frozen=True, slots=True)
class AiDecisionHistoryItem:
    record_id: str
    decided_at: int
    symbol: str
    stage: str
    provider: str
    model: str
    outcome: str
    confidence: float
    summary: str
    factors: tuple[str, ...]
    risks: tuple[str, ...]
    input_json: str
    output_json: str
    fallback: bool
    elapsed_ms: int
    response_ms: int = 0

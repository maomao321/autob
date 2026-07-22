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
    message: str = "未启动"
    updated_at: int = 0


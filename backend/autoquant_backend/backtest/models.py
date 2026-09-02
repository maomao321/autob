from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from autoquant_shared.config import AppConfig, strategy_config_snapshot
from autoquant_shared.models import Bar, Side


DAY_MS = 86_400_000
DOWNLOAD_DAYS = 180
INTERVAL_MS = {"1d": DAY_MS, "5m": 300_000, "1m": 60_000}
DOWNLOAD_INTERVALS = ("1d", "5m", "1m")
ARCHIVE_VERSION = 1
ARCHIVE_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
BAR_CSV_FIELDS = (
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
ARCHIVE_FILES = {interval: f"bars_{interval}.csv" for interval in DOWNLOAD_INTERVALS}
SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.-]{0,19}", re.ASCII)


def _decimal(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    side: str
    entry_time: int
    exit_time: int
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    pnl: Decimal
    exit_reason: str
    signal_reason: str




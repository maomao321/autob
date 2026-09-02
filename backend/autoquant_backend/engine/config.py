from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from autoquant_backend.ai_decision import (
    EntryTimingDecision,
    OpeningDecision,
)
from autoquant_shared.config import AppConfig
from autoquant_shared.models import Bar, Direction, RuntimeSnapshot, Signal


SnapshotCallback = Callable[[RuntimeSnapshot], None]
LogCallback = Callable[[str, str, str], None]
FUTURES_WARMUP_BARS = 30
FIVE_MINUTE_MS = 5 * 60 * 1000


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    app: AppConfig
    api_key: str = ""
    api_secret: str = ""
    openai_api_key: str = ""
    deepseek_api_key: str = ""
    qwen_api_key: str = ""
    manual_direction: Direction = Direction.FLAT


class OpeningDecider(Protocol):
    def decide(self, symbol: str, current_daily_bar: Bar) -> OpeningDecision:
        """Return the direction filter for one exchange trading day."""


class EntryTimingDecider(Protocol):
    def decide_entry(
        self,
        symbol: str,
        signal: Signal,
        current_bar: Bar,
        recent_bars: tuple[Bar, ...] = (),
    ) -> EntryTimingDecision:
        """Return whether the current candidate signal may enter now."""



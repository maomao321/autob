from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from autoquant_backend.ai_decision.sanitizing import _clean_text
from autoquant_shared.models import Bar, Direction, Signal

ModelInputCapture = Callable[[str, str, str, dict[str, Any]], None]
ModelOutputCapture = Callable[[str, str, str, dict[str, Any], int], None]
HistoricalBarsFetcher = Callable[[str, str, int | None, int, int], list[Bar]]
HistoricalSymbolResolver = Callable[[str], str]


class DecisionError(RuntimeError):
    """A safe, user-displayable failure while building an AI decision."""


@dataclass(frozen=True, slots=True)
class OpeningDecision:
    direction: Direction
    confidence: float
    summary: str
    factors: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    provider: str = ""
    model: str = ""
    fallback: bool = False

    @classmethod
    def flat(
        cls,
        summary: str,
        *,
        provider: str,
        model: str = "",
        risks: tuple[str, ...] = (),
    ) -> OpeningDecision:
        return cls(
            direction=Direction.FLAT,
            confidence=0.0,
            summary=_clean_text(summary, 500),
            risks=risks,
            provider=provider,
            model=model,
            fallback=True,
        )


@dataclass(frozen=True, slots=True)
class EntryTimingDecision:
    enter_now: bool
    confidence: float
    summary: str
    factors: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    provider: str = ""
    model: str = ""
    fallback: bool = False

    @classmethod
    def wait(
        cls,
        summary: str,
        *,
        provider: str,
        model: str = "",
        risks: tuple[str, ...] = (),
        fallback: bool = True,
    ) -> EntryTimingDecision:
        return cls(
            enter_now=False,
            confidence=0.0,
            summary=_clean_text(summary, 500),
            risks=risks,
            provider=provider,
            model=model,
            fallback=fallback,
        )


class DecisionClient(Protocol):
    provider: str
    model: str

    def decide(self, context: dict[str, Any]) -> OpeningDecision:
        """Return one validated, structured opening decision."""

    def decide_entry(self, context: dict[str, Any]) -> EntryTimingDecision:
        """Return one validated decision for the current candidate entry."""


class MarketContextCollector(Protocol):
    def collect(self, symbol: str, current_daily_bar: Bar) -> dict[str, Any]:
        """Collect recent news, broad-market trends and symbol trends."""


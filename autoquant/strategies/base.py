from __future__ import annotations

from abc import ABC, abstractmethod

from autoquant.models import Bar, Signal


class Strategy(ABC):
    name: str

    @abstractmethod
    def on_bar(self, bar: Bar) -> Signal | None:
        """Consume a bar update and optionally return an entry signal."""

    @abstractmethod
    def mark_executed(self, signal: Signal) -> None:
        """Record an accepted paper/live order for daily risk limits."""


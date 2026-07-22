from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from threading import Event

from autoquant.models import Bar, OrderRequest, OrderResult


StatusCallback = Callable[[str], None]


class TradingProvider(ABC):
    name: str

    @abstractmethod
    def stream_bars(
        self,
        symbol: str,
        stop_event: Event,
        status_callback: StatusCallback | None = None,
    ) -> Iterator[Bar]:
        """Yield real-time bar updates until stop_event is set."""

    @abstractmethod
    def place_order(self, order: OrderRequest) -> OrderResult:
        """Place a paper or live order."""

    @abstractmethod
    def check_symbol(self, symbol: str) -> dict:
        """Return exchange information for one symbol."""


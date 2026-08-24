from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from decimal import Decimal
from threading import Event

from autoquant_shared.models import Bar, OrderRequest, OrderResult


StatusCallback = Callable[[str], None]


class TradingProvider(ABC):
    name: str
    supports_short: bool = False
    quote_asset: str = "USDC"

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

    def get_order_detail(self, order_id: str, symbol: str = "") -> dict:
        raise NotImplementedError("当前行情源不支持订单状态查询")

    def get_account_total(self, quote_asset: str = "USDC") -> Decimal:
        raise NotImplementedError("当前供应商不支持账户总金额查询")

    def get_latest_price(self, symbol: str) -> Decimal:
        raise NotImplementedError("当前供应商不支持最新报价查询")

    def get_historical_bars(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        limit: int,
    ) -> list[Bar]:
        """Return closed historical bars in chronological order when supported."""
        return []

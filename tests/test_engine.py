from __future__ import annotations

import unittest
from decimal import Decimal
from threading import Event

from autoquant.config import AppConfig
from autoquant.engine import RunnerConfig, SymbolRunner
from autoquant.models import Bar, OrderResult


def make_bar(
    close: str,
    index: int,
    interval: str = "5m",
    open_price: str | None = None,
) -> Bar:
    price = Decimal(close)
    return Bar(
        symbol="AAPL",
        interval=interval,
        open_time=index * 300_000,
        close_time=(index + 1) * 300_000 - 1,
        open=Decimal(open_price or close),
        high=price + Decimal("0.5"),
        low=price - Decimal("0.5"),
        close=price,
        closed=True,
    )


class FakeProvider:
    def __init__(self) -> None:
        self.orders = []

    def check_symbol(self, symbol: str) -> dict:
        return {"symbol": symbol, "tradability": "BUY_SELL"}

    def stream_bars(self, symbol: str, stop_event: Event, status_callback=None):
        yield make_bar("101", 100, interval="1d", open_price="100")
        for index in range(3):
            yield make_bar("10", index)
        yield make_bar("12", 3)

    def place_order(self, order):
        self.orders.append(order)
        return OrderResult(True, "paper-test", "ok", True)


class SymbolRunnerTests(unittest.TestCase):
    def test_runner_turns_strategy_signal_into_paper_order(self) -> None:
        snapshots = []
        logs = []
        runner = SymbolRunner(
            "AAPL",
            RunnerConfig(
                AppConfig(
                    symbols=["AAPL"],
                    ma_period=3,
                    buy_notional="100",
                    sell_quantity="1",
                )
            ),
            snapshots.append,
            lambda level, symbol, message: logs.append((level, symbol, message)),
        )
        fake_provider = FakeProvider()
        runner.provider = fake_provider

        runner.start()
        runner.join(timeout=2)

        self.assertFalse(runner.is_alive)
        self.assertEqual(1, len(fake_provider.orders))
        self.assertTrue(any(level == "ORDER" for level, _symbol, _message in logs))
        self.assertEqual(1, max(snapshot.trades_today for snapshot in snapshots))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
import tempfile
from decimal import Decimal
from pathlib import Path
from threading import Event

from autoquant.config import AppConfig
from autoquant.engine import RunnerConfig, SymbolRunner
from autoquant.models import Bar, OrderResult
from autoquant.state import OrderLedger


def make_bar(
    close: str,
    index: int,
    interval: str = "5m",
    open_price: str | None = None,
) -> Bar:
    price = Decimal(close)
    if interval == "1d":
        open_time = index * 86_400_000
        close_time = open_time + 86_400_000 - 1
    else:
        open_time = index * 300_000
        close_time = (index + 1) * 300_000 - 1
    return Bar(
        symbol="AAPL",
        interval=interval,
        open_time=open_time,
        close_time=close_time,
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
        yield make_bar("101", 0, interval="1d", open_price="100")
        for index in range(3):
            yield make_bar("10", index)
        yield make_bar("12", 3)

    def place_order(self, order):
        self.orders.append(order)
        return OrderResult(True, "paper-test", "ok", True)


class ShortSignalProvider(FakeProvider):
    supports_short = False

    def stream_bars(self, symbol: str, stop_event: Event, status_callback=None):
        yield make_bar("99", 0, interval="1d", open_price="100")
        for index in range(3):
            yield make_bar("10", index)
        yield make_bar("8", 3)


class UnknownResultProvider(FakeProvider):
    def place_order(self, order):
        self.orders.append(order)
        raise RuntimeError("connection timed out")


class SymbolRunnerTests(unittest.TestCase):
    def test_runner_turns_strategy_signal_into_paper_order(self) -> None:
        snapshots = []
        logs = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
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
                ledger,
            )
            fake_provider = FakeProvider()
            runner.provider = fake_provider

            runner.start()
            runner.join(timeout=2)

            self.assertFalse(runner.is_alive)
            self.assertEqual(1, len(fake_provider.orders))
            self.assertTrue(any(level == "ORDER" for level, _symbol, _message in logs))
            self.assertEqual(1, max(snapshot.trades_today for snapshot in snapshots))

    def test_unknown_submission_is_persisted_and_stops_runner(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], ma_period=3)
                ),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            provider = UnknownResultProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual(1, ledger.unknown_count("AAPL"))
            self.assertEqual(1, ledger.count_consumed("AAPL", 0))
            self.assertEqual("ERROR", snapshots[-1].state.value)

    def test_real_short_signal_is_blocked_before_order_submission(self) -> None:
        logs = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        trading_mode="REAL",
                        ma_period=3,
                    ),
                    api_key="key",
                    api_secret="secret",
                ),
                lambda _snapshot: None,
                lambda level, symbol, message: logs.append(
                    (level, symbol, message)
                ),
                ledger,
            )
            provider = ShortSignalProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual([], provider.orders)
            self.assertEqual(0, ledger.count_consumed("AAPL", 0))
            self.assertTrue(any("建立空头" in message for _, _, message in logs))


if __name__ == "__main__":
    unittest.main()

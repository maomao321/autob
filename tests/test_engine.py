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


class StopDuringValidationProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.checks = 0
        self.stop_callback = lambda: None

    def check_symbol(self, symbol: str) -> dict:
        self.checks += 1
        if self.checks == 2:
            self.stop_callback()
        return super().check_symbol(symbol)


class FilledLiveProvider(FakeProvider):
    def place_order(self, order):
        self.orders.append(order)
        return OrderResult(True, "live-filled", "accepted", False)

    def get_order_detail(self, order_id: str) -> dict:
        return {
            "status": "FILLED",
            "executedQty": "0.5",
            "avgPrice": "200",
        }


class MalformedFillProvider(FilledLiveProvider):
    def get_order_detail(self, order_id: str) -> dict:
        return {"status": "FILLED"}


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

    def test_stop_during_pre_order_validation_prevents_submission(self) -> None:
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
                lambda *_args: None,
                ledger,
            )
            provider = StopDuringValidationProvider()
            provider.stop_callback = runner.stop
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual([], provider.orders)
            self.assertEqual(0, ledger.count_consumed("AAPL", 0))

    def test_unknown_live_order_hard_locks_runner_on_restart(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            from autoquant.models import OrderRequest, Side

            ledger.record_submitting(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.BUY,
                    reference_price=Decimal("100"),
                    buy_notional=Decimal("100"),
                    sell_quantity=Decimal("1"),
                    client_order_id="aq-unknown",
                ),
                0,
                paper=False,
            )
            ledger.mark_unknown("aq-unknown", "timeout")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], trading_mode="REAL", ma_period=3),
                    api_key="key",
                    api_secret="secret",
                ),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            provider = FakeProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual("ERROR", snapshots[-1].state.value)
            self.assertEqual([], provider.orders)

    def test_real_sell_signal_closes_tracked_long_position(self) -> None:
        from autoquant.models import OrderRequest, Side

        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.BUY,
                    reference_price=Decimal("100"),
                    buy_notional=Decimal("50"),
                    sell_quantity=Decimal("1"),
                    client_order_id="aq-filled-buy",
                ),
                0,
                paper=False,
            )
            ledger.mark_lifecycle(
                "aq-filled-buy",
                "FILLED",
                filled_quantity=Decimal("0.5"),
                average_price=Decimal("100"),
            )
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], trading_mode="REAL", ma_period=3),
                    api_key="key",
                    api_secret="secret",
                ),
                lambda _snapshot: None,
                lambda *_args: None,
                ledger,
            )
            provider = ShortSignalProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual(1, len(provider.orders))
            self.assertEqual(Side.SELL, provider.orders[0].side)
            self.assertEqual(Decimal("0.5"), provider.orders[0].sell_quantity)

    def test_live_ack_is_reconciled_to_filled_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], trading_mode="REAL", ma_period=3),
                    api_key="key",
                    api_secret="secret",
                ),
                lambda _snapshot: None,
                lambda *_args: None,
                ledger,
            )
            provider = FilledLiveProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            position = ledger.position_summary("AAPL", paper=False)
            self.assertEqual(Decimal("0.5"), position.quantity)
            self.assertEqual(Decimal("200"), position.average_price)
            self.assertEqual(0, ledger.pending_count("AAPL", paper=False))

    def test_unverifiable_fill_hard_locks_live_runner(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], trading_mode="REAL", ma_period=3),
                    api_key="key",
                    api_secret="secret",
                ),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            runner.provider = MalformedFillProvider()

            runner.start()
            runner.join(timeout=2)

            self.assertEqual(1, ledger.unknown_count("AAPL", paper=False))
            self.assertEqual("ERROR", snapshots[-1].state.value)


if __name__ == "__main__":
    unittest.main()

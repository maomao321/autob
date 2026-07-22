from __future__ import annotations

import unittest
from decimal import Decimal

from autoquant.models import Bar, Direction, Side
from autoquant.strategies.five_minute_breakout import FiveMinuteBreakoutStrategy


def bar(
    close: str,
    index: int,
    *,
    interval: str = "5m",
    open_price: str | None = None,
    high: str | None = None,
    low: str | None = None,
    closed: bool = True,
) -> Bar:
    price = Decimal(close)
    return Bar(
        symbol="AAPL",
        interval=interval,
        open_time=index * 300_000,
        close_time=(index + 1) * 300_000 - 1,
        open=Decimal(open_price or close),
        high=Decimal(high) if high is not None else price + Decimal("0.5"),
        low=Decimal(low) if low is not None else price - Decimal("0.5"),
        close=price,
        closed=closed,
    )


class FiveMinuteBreakoutStrategyTests(unittest.TestCase):
    def test_long_signal_requires_daily_bias_ma_cross_and_previous_high(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL", ma_period=3)
        strategy.on_bar(
            bar("101", 100, interval="1d", open_price="100", high="102", low="99")
        )
        self.assertEqual(Direction.LONG, strategy.direction)
        for index in range(3):
            self.assertIsNone(strategy.on_bar(bar("10", index)))

        signal = strategy.on_bar(bar("12", 3, high="12.2", low="9.8"))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(Side.BUY, signal.side)
        self.assertEqual(Decimal("10.66666666666666666666666667"), signal.ma_value)

    def test_short_signal_is_symmetric(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL", ma_period=3)
        strategy.on_bar(
            bar("99", 100, interval="1d", open_price="100", high="101", low="98")
        )
        for index in range(3):
            strategy.on_bar(bar("10", index))

        signal = strategy.on_bar(bar("8", 3, high="10.2", low="7.8"))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(Side.SELL, signal.side)

    def test_open_bar_and_duplicate_closed_bar_do_not_repeat_signal(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL", ma_period=3)
        strategy.on_bar(bar("101", 100, interval="1d", open_price="100"))
        for index in range(3):
            strategy.on_bar(bar("10", index))
        self.assertIsNone(strategy.on_bar(bar("12", 3, closed=False)))
        signal = strategy.on_bar(bar("12", 3))
        self.assertIsNotNone(signal)
        self.assertIsNone(strategy.on_bar(bar("13", 3)))

    def test_daily_limit_blocks_later_signals_after_execution(self) -> None:
        strategy = FiveMinuteBreakoutStrategy(
            "AAPL", ma_period=3, max_trades_per_day=1
        )
        strategy.on_bar(bar("101", 100, interval="1d", open_price="100"))
        for index in range(3):
            strategy.on_bar(bar("10", index))
        signal = strategy.on_bar(bar("12", 3))
        assert signal is not None
        strategy.mark_executed(signal)
        self.assertEqual(1, strategy.trades_today)

        strategy.on_bar(bar("10", 4))
        strategy.on_bar(bar("9", 5))
        self.assertIsNone(strategy.on_bar(bar("13", 6)))


if __name__ == "__main__":
    unittest.main()


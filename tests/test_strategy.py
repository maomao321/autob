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
        high=Decimal(high) if high is not None else price + Decimal("0.5"),
        low=Decimal(low) if low is not None else price - Decimal("0.5"),
        close=price,
        closed=closed,
    )


class FiveMinuteBreakoutStrategyTests(unittest.TestCase):
    def test_long_signal_requires_daily_bias_ma_cross_and_previous_high(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL", ma_period=3)
        strategy.on_bar(
            bar("101", 0, interval="1d", open_price="100", high="102", low="99")
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
            bar("99", 0, interval="1d", open_price="100", high="101", low="98")
        )
        for index in range(3):
            strategy.on_bar(bar("10", index))

        signal = strategy.on_bar(bar("8", 3, high="10.2", low="7.8"))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(Side.SELL, signal.side)

    def test_open_bar_and_duplicate_closed_bar_do_not_repeat_signal(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL", ma_period=3)
        strategy.on_bar(bar("101", 0, interval="1d", open_price="100"))
        for index in range(3):
            strategy.on_bar(bar("10", index))
        self.assertIsNone(strategy.on_bar(bar("12", 3, closed=False)))
        signal = strategy.on_bar(bar("12", 3))
        self.assertIsNotNone(signal)
        self.assertIsNone(strategy.on_bar(bar("13", 3)))

    def test_execution_count_is_exposed_for_engine_risk_checks(self) -> None:
        strategy = FiveMinuteBreakoutStrategy(
            "AAPL", ma_period=3, max_trades_per_day=1
        )
        strategy.on_bar(bar("101", 0, interval="1d", open_price="100"))
        for index in range(3):
            strategy.on_bar(bar("10", index))
        signal = strategy.on_bar(bar("12", 3))
        assert signal is not None
        strategy.mark_executed(signal)
        self.assertEqual(1, strategy.trades_today)

        strategy.on_bar(bar("10", 4))
        strategy.on_bar(bar("9", 5))
        self.assertIsNotNone(strategy.on_bar(bar("13", 6)))

    def test_new_daily_bar_clears_intraday_warmup(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL", ma_period=3)
        strategy.on_bar(bar("101", 0, interval="1d", open_price="100"))
        for index in range(3):
            strategy.on_bar(bar("10", index))
        self.assertEqual(3, strategy.warmup_bars)

        strategy.on_bar(bar("102", 1, interval="1d", open_price="101"))

        self.assertEqual(0, strategy.warmup_bars)
        self.assertIsNone(strategy.ma_value)
        strategy.on_bar(bar("11", 3))
        self.assertEqual(0, strategy.warmup_bars)
        strategy.on_bar(bar("11", 288))
        self.assertEqual(1, strategy.warmup_bars)

    def test_out_of_order_five_minute_bar_is_ignored(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL", ma_period=3)
        strategy.on_bar(bar("101", 0, interval="1d", open_price="100"))
        strategy.on_bar(bar("10", 1))
        strategy.on_bar(bar("11", 2))

        strategy.on_bar(bar("12", 1))

        self.assertEqual(2, strategy.warmup_bars)

    def test_restored_daily_count_does_not_hide_exit_signals(self) -> None:
        strategy = FiveMinuteBreakoutStrategy(
            "AAPL", ma_period=3, max_trades_per_day=1
        )
        strategy.on_bar(bar("101", 0, interval="1d", open_price="100"))
        strategy.restore_trade_count(0, 1)
        for index in range(3):
            strategy.on_bar(bar("10", index))

        self.assertIsNotNone(strategy.on_bar(bar("12", 3)))
        self.assertEqual(1, strategy.trades_today)


if __name__ == "__main__":
    unittest.main()

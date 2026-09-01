from __future__ import annotations

import unittest
from decimal import Decimal

from autoquant_shared.models import Bar, Direction, Side
from autoquant_backend.strategies.five_minute_breakout import FiveMinuteBreakoutStrategy


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


def seed_direction(
    strategy: FiveMinuteBreakoutStrategy, direction: Direction
) -> None:
    if direction is Direction.LONG:
        closes = ("99", "100")
    elif direction is Direction.SHORT:
        closes = ("101", "100")
    else:
        closes = ("100", "100")
    strategy.seed_daily_history(
        [
            bar(closes[0], -2, interval="1d"),
            bar(closes[1], -1, interval="1d"),
        ]
    )


def seed_long_setup(strategy: FiveMinuteBreakoutStrategy) -> None:
    closes = ["10"] * 18 + ["11"] * 6 + ["12"]
    for index, close in enumerate(closes):
        assert strategy.on_bar(bar(close, index)) is None


def seed_short_setup(strategy: FiveMinuteBreakoutStrategy) -> None:
    closes = ["10"] * 18 + ["9"] * 6 + ["8"]
    for index, close in enumerate(closes):
        assert strategy.on_bar(bar(close, index)) is None


class FiveMinuteBreakoutStrategyTests(unittest.TestCase):
    def test_manual_mode_rejects_unknown_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "manual direction"):
            FiveMinuteBreakoutStrategy(
                "AAPL", manual_direction=Direction.UNKNOWN
            )

    def test_manual_mode_uses_live_five_minute_bars_without_daily_bar(self) -> None:
        strategy = FiveMinuteBreakoutStrategy(
            "AAPL", manual_direction=Direction.LONG
        )
        seed_long_setup(strategy)

        signal = strategy.on_bar(bar("12", 25, closed=False))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertIs(Side.BUY, signal.side)
        self.assertEqual(Direction.LONG, strategy.direction)
        self.assertEqual(0, strategy.current_day_key)
        self.assertIn("手动开仓方向偏多", signal.reason)

    def test_long_signal_requires_ma7_above_ma25_and_two_bar_breakout(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL")
        strategy.on_bar(
            bar("101", 0, interval="1d", open_price="100", high="102", low="99")
        )
        seed_direction(strategy, Direction.LONG)
        self.assertEqual(Direction.LONG, strategy.direction)
        seed_long_setup(strategy)

        signal = strategy.on_bar(bar("12", 25, closed=False))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(Side.BUY, signal.side)
        self.assertEqual(Decimal("78") / Decimal("7"), signal.ma_value)
        self.assertIn("MA7", signal.reason)
        self.assertIn("MA25", signal.reason)
        self.assertIn("第二根最高价", signal.reason)

    def test_long_signal_requires_every_configured_condition(self) -> None:
        cases = (
            (["10"] * 23 + ["9", "10"], "11"),
            (["10"] * 18 + ["11"] * 7, "12"),
            (["10"] * 18 + ["11"] * 6 + ["12"], "11.5"),
        )
        for closes, latest_price in cases:
            with self.subTest(closes=closes[-2:], latest_price=latest_price):
                strategy = FiveMinuteBreakoutStrategy(
                    "AAPL", manual_direction=Direction.LONG
                )
                for index, close in enumerate(closes):
                    self.assertIsNone(strategy.on_bar(bar(close, index)))

                self.assertIsNone(
                    strategy.on_bar(bar(latest_price, 25, closed=False))
                )

    def test_short_signal_is_symmetric(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL")
        strategy.on_bar(
            bar("99", 0, interval="1d", open_price="100", high="101", low="98")
        )
        seed_direction(strategy, Direction.SHORT)
        seed_short_setup(strategy)

        signal = strategy.on_bar(bar("8", 25, closed=False))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(Side.SELL, signal.side)
        self.assertIn("第二根最低价", signal.reason)

    def test_ai_direction_overrides_the_intraday_daily_candle(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL")
        strategy.set_opening_direction(Direction.SHORT, "新闻与大盘偏空")
        strategy.on_bar(
            bar("101", 0, interval="1d", open_price="100")
        )
        seed_short_setup(strategy)

        signal = strategy.on_bar(bar("8", 25, closed=False))

        self.assertEqual(Direction.SHORT, strategy.direction)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(Side.SELL, signal.side)
        self.assertIn("大模型今日偏空", signal.reason)

    def test_manual_fallback_is_used_only_without_daily_direction(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL")
        strategy.on_bar(bar("101", 0, interval="1d", open_price="100"))
        strategy.set_fallback_direction(Direction.SHORT, "日线请求失败")

        self.assertEqual(Direction.SHORT, strategy.direction)
        self.assertEqual("MANUAL", strategy.direction_source)

        seed_direction(strategy, Direction.LONG)

        self.assertEqual(Direction.LONG, strategy.direction)
        self.assertEqual("DAILY", strategy.direction_source)

    def test_manual_fallback_is_identified_in_signal_reason(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL")
        strategy.on_bar(bar("101", 0, interval="1d", open_price="100"))
        strategy.set_fallback_direction(Direction.LONG, "日线不足两根")
        seed_long_setup(strategy)

        signal = strategy.on_bar(bar("12", 25, closed=False))

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertIn("采用手动方向偏多", signal.reason)

    def test_live_bar_can_trigger_once_and_closed_update_is_not_repeated(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL")
        strategy.on_bar(bar("101", 0, interval="1d", open_price="100"))
        seed_direction(strategy, Direction.LONG)
        seed_long_setup(strategy)
        self.assertIsNone(strategy.on_bar(bar("11.4", 25, closed=False)))
        signal = strategy.on_bar(bar("12", 25, closed=False))
        self.assertIsNotNone(signal)
        self.assertIsNone(strategy.on_bar(bar("13", 25, closed=False)))
        self.assertIsNone(strategy.on_bar(bar("13", 25)))
        self.assertIsNone(strategy.on_bar(bar("14", 25)))

    def test_later_bar_can_emit_a_new_signal(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL")
        strategy.on_bar(bar("101", 0, interval="1d", open_price="100"))
        seed_direction(strategy, Direction.LONG)
        seed_long_setup(strategy)
        signal = strategy.on_bar(bar("12", 25))
        self.assertIsNotNone(signal)

        strategy.on_bar(bar("13", 26))
        self.assertIsNotNone(strategy.on_bar(bar("14", 27, closed=False)))

    def test_new_daily_bar_clears_intraday_warmup(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL")
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

    def test_recent_model_context_keeps_latest_60_five_minute_bars(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL")
        history = [bar(str(100 + index), index) for index in range(65)]

        strategy.seed_recent_bars(history)

        self.assertEqual(60, len(strategy.recent_bars))
        self.assertEqual(history[5].open_time, strategy.recent_bars[0].open_time)
        self.assertEqual(history[-1].open_time, strategy.recent_bars[-1].open_time)

    def test_recent_model_context_capacity_is_configurable(self) -> None:
        strategy = FiveMinuteBreakoutStrategy(
            "AAPL", entry_context_bars=20
        )
        history = [bar(str(100 + index), index) for index in range(25)]

        strategy.seed_recent_bars(history)

        self.assertEqual(20, len(strategy.recent_bars))
        self.assertEqual(history[5].open_time, strategy.recent_bars[0].open_time)

    def test_out_of_order_five_minute_bar_is_ignored(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL")
        strategy.on_bar(bar("101", 0, interval="1d", open_price="100"))
        strategy.on_bar(bar("10", 1))
        strategy.on_bar(bar("11", 2))

        strategy.on_bar(bar("12", 1))

        self.assertEqual(2, strategy.warmup_bars)

    def test_direction_requires_two_previous_trading_days(self) -> None:
        strategy = FiveMinuteBreakoutStrategy("AAPL")
        strategy.on_bar(bar("120", 0, interval="1d", open_price="100"))

        self.assertEqual(Direction.UNKNOWN, strategy.direction)

        seed_direction(strategy, Direction.LONG)
        self.assertEqual(Direction.LONG, strategy.direction)

        seed_direction(strategy, Direction.SHORT)
        self.assertEqual(Direction.SHORT, strategy.direction)


if __name__ == "__main__":
    unittest.main()

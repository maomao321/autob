from __future__ import annotations

from collections import deque
from decimal import Decimal

from autoquant.models import Bar, Direction, Side, Signal
from autoquant.strategies.base import Strategy


class FiveMinuteBreakoutStrategy(Strategy):
    """Daily direction filter plus five-minute MA and prior-bar breakout."""

    name = "five_minute_breakout"

    def __init__(
        self,
        symbol: str,
        ma_period: int = 5,
        max_trades_per_day: int = 1,
    ) -> None:
        if ma_period < 2:
            raise ValueError("ma_period must be at least 2")
        self.symbol = symbol.upper()
        self.ma_period = ma_period
        self.max_trades_per_day = max_trades_per_day
        self._bars: deque[Bar] = deque(maxlen=ma_period + 1)
        self._daily_bar: Bar | None = None
        self._last_evaluated_open_time: int | None = None
        self._trades_by_day: dict[int, int] = {}
        self.last_price: Decimal | None = None
        self.ma_value: Decimal | None = None

    @property
    def direction(self) -> Direction:
        if self._daily_bar is None:
            return Direction.UNKNOWN
        if self._daily_bar.close > self._daily_bar.open:
            return Direction.LONG
        if self._daily_bar.close < self._daily_bar.open:
            return Direction.SHORT
        return Direction.FLAT

    @property
    def warmup_bars(self) -> int:
        return len(self._bars)

    @property
    def warmup_required(self) -> int:
        return self.ma_period + 1

    @property
    def trades_today(self) -> int:
        if self._daily_bar is None:
            return 0
        return self._trades_by_day.get(self._daily_bar.open_time, 0)

    @property
    def current_day_key(self) -> int | None:
        if self._daily_bar is None:
            return None
        return self._daily_bar.open_time

    def restore_trade_count(self, day_key: int, count: int) -> None:
        self._trades_by_day[day_key] = max(0, int(count))
        self._remove_old_trade_counters(day_key)

    def on_bar(self, bar: Bar) -> Signal | None:
        if bar.symbol.upper() != self.symbol:
            return None
        self.last_price = bar.close
        if bar.interval == "1d":
            if self._daily_bar is not None and bar.open_time < self._daily_bar.open_time:
                return None
            if self._daily_bar is None or bar.open_time > self._daily_bar.open_time:
                self._bars.clear()
                self._last_evaluated_open_time = None
                self.ma_value = None
            self._daily_bar = bar
            self._remove_old_trade_counters(bar.open_time)
            return None
        if bar.interval != "5m" or not bar.closed:
            return None
        if self._daily_bar is None:
            return None
        if not (
            self._daily_bar.open_time
            <= bar.open_time
            <= self._daily_bar.close_time
        ):
            return None
        if (
            self._last_evaluated_open_time is not None
            and bar.open_time <= self._last_evaluated_open_time
        ):
            return None
        self._last_evaluated_open_time = bar.open_time
        self._append_closed_bar(bar)
        if len(self._bars) < self.warmup_required:
            return None

        bars = list(self._bars)
        previous_bar = bars[-2]
        previous_window = bars[-(self.ma_period + 1) : -1]
        current_window = bars[-self.ma_period :]
        previous_ma = self._mean_close(previous_window)
        current_ma = self._mean_close(current_window)
        self.ma_value = current_ma

        day_key = self._daily_bar.open_time
        if self._trades_by_day.get(day_key, 0) >= self.max_trades_per_day:
            return None

        crossed_up = previous_bar.close <= previous_ma and bar.close > current_ma
        broke_previous_high = bar.close > previous_bar.high
        if self.direction is Direction.LONG and crossed_up and broke_previous_high:
            return Signal(
                symbol=self.symbol,
                side=Side.BUY,
                price=bar.close,
                ma_value=current_ma,
                bar_open_time=bar.open_time,
                reason=(
                    f"日线偏多；5分钟收盘价 {bar.close} 上穿 MA{self.ma_period} "
                    f"{current_ma}，并突破前一根最高价 {previous_bar.high}"
                ),
            )

        crossed_down = previous_bar.close >= previous_ma and bar.close < current_ma
        broke_previous_low = bar.close < previous_bar.low
        if self.direction is Direction.SHORT and crossed_down and broke_previous_low:
            return Signal(
                symbol=self.symbol,
                side=Side.SELL,
                price=bar.close,
                ma_value=current_ma,
                bar_open_time=bar.open_time,
                reason=(
                    f"日线偏空；5分钟收盘价 {bar.close} 下穿 MA{self.ma_period} "
                    f"{current_ma}，并跌破前一根最低价 {previous_bar.low}"
                ),
            )
        return None

    def mark_executed(self, signal: Signal) -> None:
        if self._daily_bar is None or signal.symbol != self.symbol:
            return
        day_key = self._daily_bar.open_time
        self._trades_by_day[day_key] = self._trades_by_day.get(day_key, 0) + 1

    def _append_closed_bar(self, bar: Bar) -> None:
        if self._bars and self._bars[-1].open_time == bar.open_time:
            self._bars[-1] = bar
        else:
            self._bars.append(bar)

    def _remove_old_trade_counters(self, current_day: int) -> None:
        self._trades_by_day = {
            day: count
            for day, count in self._trades_by_day.items()
            if day == current_day
        }

    @staticmethod
    def _mean_close(bars: list[Bar]) -> Decimal:
        return sum((bar.close for bar in bars), Decimal("0")) / Decimal(len(bars))

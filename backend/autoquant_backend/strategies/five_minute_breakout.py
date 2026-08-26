from __future__ import annotations

from collections import deque
from decimal import Decimal

from autoquant_shared.models import Bar, Direction, Side, Signal
from autoquant_shared.formatting import financial_text
from autoquant_backend.strategies.base import Strategy

DAY_MS = 86_400_000


class FiveMinuteBreakoutStrategy(Strategy):
    """Daily direction filter plus five-minute MA and prior-bar breakout."""

    name = "five_minute_breakout"

    def __init__(
        self,
        symbol: str,
        ma_period: int = 5,
        max_trades_per_day: int = 1,
        manual_direction: Direction | None = None,
    ) -> None:
        if ma_period < 2:
            raise ValueError("ma_period must be at least 2")
        if manual_direction not in {
            None,
            Direction.LONG,
            Direction.SHORT,
            Direction.FLAT,
        }:
            raise ValueError("manual direction must be LONG, SHORT or FLAT")
        self.symbol = symbol.upper()
        self.ma_period = ma_period
        self.max_trades_per_day = max_trades_per_day
        self._bars: deque[Bar] = deque(maxlen=ma_period + 1)
        self._daily_bar: Bar | None = None
        self._manual_day_key: int | None = None
        self._manual_direction = manual_direction
        self._direction_daily_bars: tuple[Bar, Bar] | None = None
        self._last_evaluated_open_time: int | None = None
        self._trades_by_day: dict[int, int] = {}
        self._opening_direction: Direction | None = None
        self._opening_direction_reason = ""
        self._fallback_direction: Direction | None = None
        self._fallback_direction_reason = ""
        self.last_price: Decimal | None = None
        self.ma_value: Decimal | None = None

    @property
    def direction(self) -> Direction:
        if self._manual_direction is not None:
            return self._manual_direction
        if self._opening_direction is not None:
            return self._opening_direction
        if self._direction_daily_bars is None:
            return self._fallback_direction or Direction.UNKNOWN
        older, newer = self._direction_daily_bars
        if newer.close > older.close:
            return Direction.LONG
        if newer.close < older.close:
            return Direction.SHORT
        return Direction.FLAT

    @property
    def opening_direction_reason(self) -> str:
        return self._opening_direction_reason

    @property
    def direction_source(self) -> str:
        if self._manual_direction is not None:
            return "MANUAL"
        if self._opening_direction is not None:
            return "MODEL"
        if self._direction_daily_bars is not None:
            return "DAILY"
        if self._fallback_direction is not None:
            return "MANUAL"
        return "UNKNOWN"

    def set_opening_direction(
        self, direction: Direction, reason: str = ""
    ) -> None:
        if direction not in {
            Direction.LONG,
            Direction.SHORT,
            Direction.FLAT,
        }:
            raise ValueError("opening direction must be LONG, SHORT or FLAT")
        self._opening_direction = direction
        self._opening_direction_reason = " ".join(str(reason).split())[:500]

    def set_fallback_direction(
        self, direction: Direction, reason: str = ""
    ) -> None:
        if direction not in {
            Direction.LONG,
            Direction.SHORT,
            Direction.FLAT,
        }:
            raise ValueError("fallback direction must be LONG, SHORT or FLAT")
        self._fallback_direction = direction
        self._fallback_direction_reason = " ".join(str(reason).split())[:500]

    def seed_daily_history(self, bars: list[Bar]) -> None:
        eligible = sorted(
            (
                bar
                for bar in bars
                if bar.symbol.upper() == self.symbol
                and bar.interval == "1d"
                and bar.closed
                and (
                    self._daily_bar is None
                    or bar.open_time < self._daily_bar.open_time
                )
            ),
            key=lambda bar: bar.open_time,
        )
        self._direction_daily_bars = (
            (eligible[-2], eligible[-1]) if len(eligible) >= 2 else None
        )

    @property
    def warmup_bars(self) -> int:
        return len(self._bars)

    @property
    def warmup_required(self) -> int:
        return self.ma_period + 1

    @property
    def recent_bars(self) -> tuple[Bar, ...]:
        return tuple(self._bars)

    @property
    def trades_today(self) -> int:
        day_key = self.current_day_key
        if day_key is None:
            return 0
        return self._trades_by_day.get(day_key, 0)

    @property
    def current_day_key(self) -> int | None:
        if self._manual_direction is not None:
            return self._manual_day_key
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
            if self._manual_direction is not None:
                return None
            if self._daily_bar is not None and bar.open_time < self._daily_bar.open_time:
                return None
            if self._daily_bar is None or bar.open_time > self._daily_bar.open_time:
                self._bars.clear()
                self._direction_daily_bars = None
                self._fallback_direction = None
                self._fallback_direction_reason = ""
                self._last_evaluated_open_time = None
                self.ma_value = None
            self._daily_bar = bar
            self._remove_old_trade_counters(bar.open_time)
            return None
        if bar.interval != "5m" or not bar.closed:
            return None
        if self._manual_direction is not None:
            day_key = bar.open_time - (bar.open_time % DAY_MS)
            if self._manual_day_key is None or day_key > self._manual_day_key:
                self._reset_intraday_state()
                self._manual_day_key = day_key
                self._remove_old_trade_counters(day_key)
            elif day_key < self._manual_day_key:
                return None
        else:
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

        crossed_up = previous_bar.close <= previous_ma and bar.close > current_ma
        broke_previous_high = bar.close > previous_bar.high
        if self.direction is Direction.LONG and crossed_up and broke_previous_high:
            direction_reason = self._direction_reason(long=True)
            return Signal(
                symbol=self.symbol,
                side=Side.BUY,
                price=bar.close,
                ma_value=current_ma,
                bar_open_time=bar.open_time,
                reason=(
                    f"{direction_reason}；5分钟收盘价 {financial_text(bar.close)} "
                    f"上穿 MA{self.ma_period} "
                    f"{financial_text(current_ma)}，并突破前一根最高价 "
                    f"{financial_text(previous_bar.high)}"
                ),
            )

        crossed_down = previous_bar.close >= previous_ma and bar.close < current_ma
        broke_previous_low = bar.close < previous_bar.low
        if self.direction is Direction.SHORT and crossed_down and broke_previous_low:
            direction_reason = self._direction_reason(long=False)
            return Signal(
                symbol=self.symbol,
                side=Side.SELL,
                price=bar.close,
                ma_value=current_ma,
                bar_open_time=bar.open_time,
                reason=(
                    f"{direction_reason}；5分钟收盘价 {financial_text(bar.close)} "
                    f"下穿 MA{self.ma_period} "
                    f"{financial_text(current_ma)}，并跌破前一根最低价 "
                    f"{financial_text(previous_bar.low)}"
                ),
            )
        return None

    def _direction_reason(self, *, long: bool) -> str:
        bias = "偏多" if long else "偏空"
        if self._manual_direction is not None:
            return f"手动开仓方向{bias}"
        if self._opening_direction is not None:
            return f"大模型今日{bias}（{self._opening_direction_reason}）"
        if self._direction_daily_bars is not None:
            return f"前两交易日收盘趋势{bias}"
        if self._fallback_direction is not None:
            detail = (
                f"（{self._fallback_direction_reason}）"
                if self._fallback_direction_reason
                else ""
            )
            return f"日线不可用，采用手动方向{bias}{detail}"
        return f"今日方向{bias}"

    def mark_executed(self, signal: Signal) -> None:
        day_key = self.current_day_key
        if day_key is None or signal.symbol != self.symbol:
            return
        self._trades_by_day[day_key] = self._trades_by_day.get(day_key, 0) + 1

    def _reset_intraday_state(self) -> None:
        self._bars.clear()
        self._last_evaluated_open_time = None
        self.ma_value = None

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

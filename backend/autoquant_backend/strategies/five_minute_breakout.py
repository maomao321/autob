from __future__ import annotations

from collections import deque
from decimal import Decimal

from autoquant_shared.models import Bar, Direction, Side, Signal
from autoquant_shared.formatting import financial_text
from autoquant_backend.strategies.base import Strategy

DAY_MS = 86_400_000
AI_ENTRY_CONTEXT_BARS = 60
FAST_MA_PERIOD = 7
SLOW_MA_PERIOD = 25


class FiveMinuteBreakoutStrategy(Strategy):
    """Daily direction filter plus MA7/MA25 two-bar breakout."""

    name = "five_minute_breakout"

    def __init__(
        self,
        symbol: str,
        ma_period: int | None = None,
        manual_direction: Direction | None = None,
        entry_context_bars: int = AI_ENTRY_CONTEXT_BARS,
    ) -> None:
        # Keep accepting the legacy argument so older integrations continue to
        # load, but this strategy now always uses the fixed MA7/MA25 pair.
        if ma_period is not None and int(ma_period) < 2:
            raise ValueError("ma_period must be at least 2")
        if manual_direction not in {
            None,
            Direction.LONG,
            Direction.SHORT,
            Direction.FLAT,
        }:
            raise ValueError("manual direction must be LONG, SHORT or FLAT")
        if not 10 <= int(entry_context_bars) <= 300:
            raise ValueError("entry_context_bars must be between 10 and 300")
        self.symbol = symbol.upper()
        self.ma_period = SLOW_MA_PERIOD
        self.fast_ma_period = FAST_MA_PERIOD
        self.slow_ma_period = SLOW_MA_PERIOD
        self._bars: deque[Bar] = deque(maxlen=self.slow_ma_period)
        self.entry_context_bars = int(entry_context_bars)
        self._recent_bars: deque[Bar] = deque(maxlen=self.entry_context_bars)
        self._daily_bar: Bar | None = None
        self._manual_day_key: int | None = None
        self._manual_direction = manual_direction
        self._direction_daily_bars: tuple[Bar, Bar] | None = None
        self._last_evaluated_open_time: int | None = None
        self._last_signaled_open_time: int | None = None
        self._opening_direction: Direction | None = None
        self._opening_direction_reason = ""
        self._fallback_direction: Direction | None = None
        self._fallback_direction_reason = ""
        self.last_price: Decimal | None = None
        self.ma_value: Decimal | None = None
        self.fast_ma_value: Decimal | None = None
        self.slow_ma_value: Decimal | None = None

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
        return self.slow_ma_period

    @property
    def recent_bars(self) -> tuple[Bar, ...]:
        return tuple(self._recent_bars)

    def seed_recent_bars(self, bars: list[Bar]) -> None:
        """Seed the model's rolling 5-minute OHLC context without trading."""
        eligible = sorted(
            {
                bar.open_time: bar
                for bar in bars
                if bar.symbol.upper() == self.symbol
                and bar.interval == "5m"
                and bar.closed
            }.values(),
            key=lambda bar: bar.open_time,
        )
        self._recent_bars.clear()
        self._recent_bars.extend(eligible[-self.entry_context_bars :])

    @property
    def current_day_key(self) -> int | None:
        if self._manual_direction is not None:
            return self._manual_day_key
        if self._daily_bar is None:
            return None
        return self._daily_bar.open_time

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
                self._last_signaled_open_time = None
                self.ma_value = None
                self.fast_ma_value = None
                self.slow_ma_value = None
            self._daily_bar = bar
            return None
        if bar.interval != "5m":
            return None
        if self._manual_direction is not None:
            day_key = bar.open_time - (bar.open_time % DAY_MS)
            if self._manual_day_key is None or day_key > self._manual_day_key:
                self._reset_intraday_state()
                self._manual_day_key = day_key
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

        signal = self._signal_for_latest_price(bar)
        if bar.closed:
            self._last_evaluated_open_time = bar.open_time
            self._append_closed_bar(bar)
        return signal

    def _signal_for_latest_price(self, bar: Bar) -> Signal | None:
        if (
            len(self._bars) < self.warmup_required
            or self._last_signaled_open_time == bar.open_time
        ):
            return None

        bars = list(self._bars)
        first_bar = bars[-1]
        second_bar = bars[-2]
        fast_ma = self._mean_close(bars[-self.fast_ma_period :])
        slow_ma = self._mean_close(bars[-self.slow_ma_period :])
        self.fast_ma_value = fast_ma
        self.slow_ma_value = slow_ma
        self.ma_value = fast_ma

        if (
            self.direction is Direction.LONG
            and fast_ma > slow_ma
            and first_bar.close > second_bar.close
            and bar.close > second_bar.high
        ):
            self._last_signaled_open_time = bar.open_time
            return Signal(
                symbol=self.symbol,
                side=Side.BUY,
                price=bar.close,
                ma_value=fast_ma,
                bar_open_time=bar.open_time,
                reason=(
                    f"{self._direction_reason(long=True)}；"
                    f"MA7 {financial_text(fast_ma)} 高于 MA25 "
                    f"{financial_text(slow_ma)}；第一根5分钟K线收盘价 "
                    f"{financial_text(first_bar.close)} 高于第二根收盘价 "
                    f"{financial_text(second_bar.close)}；最新价 "
                    f"{financial_text(bar.close)} 突破第二根最高价 "
                    f"{financial_text(second_bar.high)}"
                ),
                strategy_context=self._entry_decision_context(
                    bar=bar,
                    first_bar=first_bar,
                    second_bar=second_bar,
                    fast_ma=fast_ma,
                    slow_ma=slow_ma,
                    long=True,
                ),
            )

        if (
            self.direction is Direction.SHORT
            and fast_ma < slow_ma
            and first_bar.close < second_bar.close
            and bar.close < second_bar.low
        ):
            self._last_signaled_open_time = bar.open_time
            return Signal(
                symbol=self.symbol,
                side=Side.SELL,
                price=bar.close,
                ma_value=fast_ma,
                bar_open_time=bar.open_time,
                reason=(
                    f"{self._direction_reason(long=False)}；"
                    f"MA7 {financial_text(fast_ma)} 低于 MA25 "
                    f"{financial_text(slow_ma)}；第一根5分钟K线收盘价 "
                    f"{financial_text(first_bar.close)} 低于第二根收盘价 "
                    f"{financial_text(second_bar.close)}；最新价 "
                    f"{financial_text(bar.close)} 跌破第二根最低价 "
                    f"{financial_text(second_bar.low)}"
                ),
                strategy_context=self._entry_decision_context(
                    bar=bar,
                    first_bar=first_bar,
                    second_bar=second_bar,
                    fast_ma=fast_ma,
                    slow_ma=slow_ma,
                    long=False,
                ),
            )
        return None

    def _entry_decision_context(
        self,
        *,
        bar: Bar,
        first_bar: Bar,
        second_bar: Bar,
        fast_ma: Decimal,
        slow_ma: Decimal,
        long: bool,
    ) -> dict[str, object]:
        breakout_level = second_bar.high if long else second_bar.low
        breakout_distance = (
            bar.close - breakout_level
            if long
            else breakout_level - bar.close
        )
        breakout_percent = (
            breakout_distance / breakout_level * Decimal("100")
            if breakout_level > 0
            else Decimal("0")
        )
        return {
            "strategy": {
                "strategy_id": self.name,
                "strategy_name": "五分钟 MA7/MA25 双 K 线突破",
                "description": (
                    "先按当日方向过滤，再要求快慢均线同向、最近两根已收盘"
                    "五分钟 K 线延续，最后由候选价格突破参考高点或低点。"
                ),
                "bar_interval": "5m",
                "direction": self.direction.value,
                "direction_source": self.direction_source,
                "direction_reason": self._direction_reason(long=long),
                "parameters": {
                    "fast_ma_period": self.fast_ma_period,
                    "slow_ma_period": self.slow_ma_period,
                    "entry_context_bars": self.entry_context_bars,
                },
                "rules": {
                    "direction_filter": "LONG 只允许 BUY；SHORT 只允许 SELL",
                    "ma_filter": (
                        "MA7 > MA25" if long else "MA7 < MA25"
                    ),
                    "closed_bar_confirmation": (
                        "最近已收盘 K 线 close > 前一根 close"
                        if long
                        else "最近已收盘 K 线 close < 前一根 close"
                    ),
                    "breakout_trigger": (
                        "候选价格 > 前一根已收盘 K 线 high"
                        if long
                        else "候选价格 < 前一根已收盘 K 线 low"
                    ),
                },
                "indicator_state": {
                    "fast_ma_value": financial_text(fast_ma),
                    "slow_ma_value": financial_text(slow_ma),
                    "ma_spread": financial_text(fast_ma - slow_ma),
                    "warmup_bars": self.warmup_bars,
                    "warmup_required": self.warmup_required,
                },
            },
            "signal": {
                "signal_type": (
                    "LONG_BREAKOUT" if long else "SHORT_BREAKDOWN"
                ),
                "implied_direction": "LONG" if long else "SHORT",
                "breakout_level": financial_text(breakout_level),
                "breakout_distance": financial_text(breakout_distance),
                "breakout_distance_percent": financial_text(
                    breakout_percent
                ),
                "latest_closed_bar": self._context_bar(first_bar),
                "reference_closed_bar": self._context_bar(second_bar),
                "trigger_conditions": {
                    "direction_matches": True,
                    "ma_condition_met": True,
                    "closed_bar_confirmation_met": True,
                    "breakout_condition_met": True,
                },
            },
        }

    @staticmethod
    def _context_bar(bar: Bar) -> dict[str, object]:
        return {
            "open_time_ms": bar.open_time,
            "close_time_ms": bar.close_time,
            "open": financial_text(bar.open),
            "high": financial_text(bar.high),
            "low": financial_text(bar.low),
            "close": financial_text(bar.close),
            "is_closed": bar.closed,
        }

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
        # Position-cycle limits are derived atomically from the persistent
        # order ledger, so this strategy does not keep an in-memory counter.
        return None

    def _reset_intraday_state(self) -> None:
        self._bars.clear()
        self._last_evaluated_open_time = None
        self._last_signaled_open_time = None
        self.ma_value = None
        self.fast_ma_value = None
        self.slow_ma_value = None

    def _append_closed_bar(self, bar: Bar) -> None:
        if self._bars and self._bars[-1].open_time == bar.open_time:
            self._bars[-1] = bar
        else:
            self._bars.append(bar)
        if self._recent_bars and self._recent_bars[-1].open_time == bar.open_time:
            self._recent_bars[-1] = bar
        elif not self._recent_bars or self._recent_bars[-1].open_time < bar.open_time:
            self._recent_bars.append(bar)

    @staticmethod
    def _mean_close(bars: list[Bar]) -> Decimal:
        return sum((bar.close for bar in bars), Decimal("0")) / Decimal(len(bars))

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any

from autoquant_backend.backtest.models import BacktestTrade
from autoquant_backend.backtest.store import BacktestStore
from autoquant_backend.strategies.five_minute_breakout import FiveMinuteBreakoutStrategy
from autoquant_shared.config import AppConfig
from autoquant_shared.models import Bar, Side

class BacktestCancelled(RuntimeError):
    pass


class BacktestService:
    def __init__(self, store: BacktestStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._cancel_events: dict[str, threading.Event] = {}

    def start(
        self,
        provider: str,
        symbol: str,
        strategy: str,
        config: AppConfig,
        *,
        download_id: str = "",
    ) -> str:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("回测标的不能为空")
        if strategy != "five_minute_breakout":
            raise ValueError(f"暂不支持回测策略: {strategy}")
        download = (
            self.store.get_download(download_id)
            if download_id.strip()
            else self.store.latest_complete_download(provider, normalized)
        )
        if download is None:
            raise ValueError("请先完成该标的历史 K 线下载或导入")
        if str(download.get("provider", "")) != provider:
            raise ValueError("历史 K 线记录与当前行情源不一致")
        if str(download.get("symbol", "")).upper() != normalized:
            raise ValueError("历史 K 线记录与回测标的不一致")
        if str(download.get("status", "")) != "COMPLETED":
            raise ValueError("所选历史 K 线记录尚未完成")
        selected_download_id = str(download.get("download_id", ""))
        with self._lock:
            if self.store.has_active_run_for_download(selected_download_id):
                raise ValueError("所选历史 K 线记录已有回测正在执行")
            run_id = self.store.create_run(
                provider,
                normalized,
                strategy,
                int(download["start_time"]),
                int(download["end_time"]),
                config,
                selected_download_id,
            )
            cancel_event = threading.Event()
            self._cancel_events[run_id] = cancel_event
        threading.Thread(
            target=self._run,
            args=(run_id, provider, normalized, download, config, cancel_event),
            name=f"backtest-run-{normalized}",
            daemon=True,
        ).start()
        return run_id

    def cancel(self, run_id: str) -> None:
        normalized = run_id.strip()
        with self._lock:
            cancel_event = self._cancel_events.get(normalized)
        if cancel_event is None:
            raise ValueError("回测任务不存在或已经结束")
        if not self.store.request_run_cancel(normalized):
            raise ValueError("回测任务已经结束")
        cancel_event.set()

    def _run(
        self,
        run_id: str,
        provider: str,
        symbol: str,
        download: dict[str, Any],
        config: AppConfig,
        cancel_event: threading.Event | None = None,
    ) -> None:
        cancel_event = cancel_event or threading.Event()
        try:
            if cancel_event.is_set():
                raise BacktestCancelled
            self.store.update_run(run_id, "RUNNING", "正在执行回测")
            start_time = int(download["start_time"])
            end_time = int(download["end_time"])
            daily = self.store.load_bars(
                provider, symbol, "1d", start_time, end_time
            )
            if cancel_event.is_set():
                raise BacktestCancelled
            five = self.store.load_bars(
                provider, symbol, "5m", start_time, end_time
            )
            if cancel_event.is_set():
                raise BacktestCancelled
            minute = self.store.load_bars(
                provider, symbol, "1m", start_time, end_time
            )
            trades = self._simulate(
                symbol,
                daily,
                five,
                minute,
                config,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                raise BacktestCancelled
            total_pnl = sum((trade.pnl for trade in trades), Decimal("0"))
            capital = Decimal(config.buy_notional)
            return_percent = (
                total_pnl / capital * Decimal("100")
                if capital > 0
                else Decimal("0")
            )
            equity = capital
            peak = capital
            max_drawdown = Decimal("0")
            for trade in trades:
                equity += trade.pnl
                peak = max(peak, equity)
                if peak > 0:
                    max_drawdown = max(
                        max_drawdown, (peak - equity) / peak * Decimal("100")
                    )
            completed = self.store.complete_run(
                run_id,
                trades,
                total_pnl=total_pnl,
                return_percent=return_percent,
                max_drawdown_percent=max_drawdown,
            )
            if not completed:
                raise BacktestCancelled
        except BacktestCancelled:
            self.store.update_run(run_id, "CANCELLED", "回测已停止")
        except Exception as exc:
            if cancel_event.is_set():
                self.store.update_run(run_id, "CANCELLED", "回测已停止")
            else:
                self.store.update_run(
                    run_id, "FAILED", str(exc) or exc.__class__.__name__
                )
        finally:
            with self._lock:
                self._cancel_events.pop(run_id, None)

    @staticmethod
    def _simulate(
        symbol: str,
        daily: list[Bar],
        five: list[Bar],
        minute: list[Bar],
        config: AppConfig,
        *,
        cancel_event: threading.Event | None = None,
    ) -> list[BacktestTrade]:
        strategy = FiveMinuteBreakoutStrategy(
            symbol=symbol,
            manual_direction=None,
            entry_context_bars=config.ai_entry_timing_bars,
        )
        stop_fraction = Decimal(config.stop_loss_percent) / Decimal("100")
        take_fraction = Decimal(config.take_profit_percent) / Decimal("100")
        notional = Decimal(config.buy_notional)
        trades: list[BacktestTrade] = []
        position: dict[str, Any] | None = None
        day_index = -1
        minute_index = 0

        def close_position(bar: Bar, price: Decimal, reason: str) -> None:
            nonlocal position
            if position is None:
                return
            multiplier = Decimal("1") if position["side"] == "LONG" else Decimal("-1")
            pnl = (price - position["entry_price"]) * position["quantity"] * multiplier
            trades.append(
                BacktestTrade(
                    side=position["side"],
                    entry_time=position["entry_time"],
                    exit_time=bar.close_time,
                    entry_price=position["entry_price"],
                    exit_price=price,
                    quantity=position["quantity"],
                    pnl=pnl,
                    exit_reason=reason,
                    signal_reason=position["signal_reason"],
                )
            )
            position = None

        for five_bar in five:
            if cancel_event is not None and cancel_event.is_set():
                raise BacktestCancelled
            while minute_index < len(minute) and minute[minute_index].close_time <= five_bar.close_time:
                if cancel_event is not None and cancel_event.is_set():
                    raise BacktestCancelled
                minute_bar = minute[minute_index]
                minute_index += 1
                if position is None or minute_bar.open_time <= position["entry_time"]:
                    continue
                if position["side"] == "LONG":
                    stop_price = position["entry_price"] * (Decimal("1") - stop_fraction)
                    take_price = position["entry_price"] * (Decimal("1") + take_fraction)
                    if minute_bar.low <= stop_price:
                        close_position(minute_bar, stop_price, "STOP_LOSS")
                    elif minute_bar.high >= take_price:
                        close_position(minute_bar, take_price, "TAKE_PROFIT")
                else:
                    stop_price = position["entry_price"] * (Decimal("1") + stop_fraction)
                    take_price = position["entry_price"] * (Decimal("1") - take_fraction)
                    if minute_bar.high >= stop_price:
                        close_position(minute_bar, stop_price, "STOP_LOSS")
                    elif minute_bar.low <= take_price:
                        close_position(minute_bar, take_price, "TAKE_PROFIT")

            while day_index + 1 < len(daily) and daily[day_index + 1].open_time <= five_bar.open_time:
                next_day = daily[day_index + 1]
                if next_day.close_time < five_bar.open_time:
                    day_index += 1
                    continue
                day_index += 1
                strategy.on_bar(next_day)
                strategy.seed_daily_history(daily[:day_index])
                break
            if day_index < 0 or not (
                daily[day_index].open_time <= five_bar.open_time <= daily[day_index].close_time
            ):
                continue
            signal = strategy.on_bar(five_bar)
            if signal is None:
                continue
            signal_side = "LONG" if signal.side is Side.BUY else "SHORT"
            added_quantity = notional / signal.price
            if position is None:
                position = {
                    "side": signal_side,
                    "entry_time": five_bar.close_time,
                    "entry_price": signal.price,
                    "quantity": added_quantity,
                    "additions": 0,
                    "signal_reason": signal.reason,
                }
                continue
            if (
                position["side"] != signal_side
                or position["additions"]
                >= config.max_additions_per_position
            ):
                continue
            previous_cost = position["entry_price"] * position["quantity"]
            position["quantity"] += added_quantity
            position["entry_price"] = (
                previous_cost + signal.price * added_quantity
            ) / position["quantity"]
            position["additions"] += 1
            position["signal_reason"] += f"；第 {position['additions']} 次加仓：{signal.reason}"

        if position is not None:
            final_bar = minute[-1] if minute else five[-1]
            close_position(final_bar, final_bar.close, "END_OF_DATA")
        return trades


__all__ = [
    "BacktestService",
    "BacktestStore",
    "BacktestTrade",
    "HistoricalArchiveService",
    "HistoricalDownloader",
]


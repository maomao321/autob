from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Callable

from autoquant.config import AppConfig
from autoquant.models import (
    OrderRequest,
    RunState,
    RuntimeSnapshot,
)
from autoquant.providers.base import TradingProvider
from autoquant.providers.binance_stocks import BinanceStocksProvider
from autoquant.strategies.base import Strategy
from autoquant.strategies.five_minute_breakout import FiveMinuteBreakoutStrategy


SnapshotCallback = Callable[[RuntimeSnapshot], None]
LogCallback = Callable[[str, str, str], None]


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    app: AppConfig
    api_key: str = ""
    api_secret: str = ""


def create_provider(config: RunnerConfig) -> TradingProvider:
    if config.app.provider == "binance_stocks":
        return BinanceStocksProvider(
            api_key=config.api_key,
            api_secret=config.api_secret,
            live_trading=config.app.trading_mode == "REAL",
            rest_base_url=config.app.rest_base_url,
            websocket_base_url=config.app.websocket_base_url,
            recv_window=config.app.recv_window,
        )
    raise ValueError(f"未知 API 供应商: {config.app.provider}")


def create_strategy(symbol: str, config: RunnerConfig) -> Strategy:
    if config.app.strategy == "five_minute_breakout":
        return FiveMinuteBreakoutStrategy(
            symbol=symbol,
            ma_period=config.app.ma_period,
            max_trades_per_day=config.app.max_trades_per_day,
        )
    raise ValueError(f"未知策略: {config.app.strategy}")


class SymbolRunner:
    def __init__(
        self,
        symbol: str,
        config: RunnerConfig,
        snapshot_callback: SnapshotCallback,
        log_callback: LogCallback,
    ) -> None:
        self.symbol = symbol.upper()
        self.config = config
        self.provider = create_provider(config)
        self.strategy = create_strategy(self.symbol, config)
        self.snapshot_callback = snapshot_callback
        self.log_callback = log_callback
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._snapshot = RuntimeSnapshot(symbol=self.symbol)
        self._snapshot_lock = threading.Lock()

    @property
    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        if self.is_alive:
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name=f"autoquant-{self.symbol}",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        if not self.is_alive:
            self._update(RunState.STOPPED, "已停止")
            return
        self._update(RunState.STOPPING, "正在停止")
        self.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        if self.thread:
            self.thread.join(timeout=timeout)

    def snapshot(self) -> RuntimeSnapshot:
        with self._snapshot_lock:
            return replace(self._snapshot)

    def _run(self) -> None:
        self._update(RunState.STARTING, "正在校验股票并连接行情")
        try:
            info = self.provider.check_symbol(self.symbol)
            validation = info.get("validation")
            if validation:
                self._log("INFO", str(validation))
            else:
                self._log(
                    "INFO",
                    f"股票校验通过，tradability={info.get('tradability', 'UNKNOWN')}",
                )
            self._update(RunState.WARMING_UP, "等待日线和 5 分钟 K 线")
            for bar in self.provider.stream_bars(
                self.symbol, self.stop_event, self._on_provider_status
            ):
                if self.stop_event.is_set():
                    break
                signal = self.strategy.on_bar(bar)
                self._refresh_market_snapshot()
                if signal is None:
                    continue

                self._update(RunState.SIGNAL, signal.reason)
                self._log("SIGNAL", signal.reason)
                if self.config.app.trading_mode == "REAL":
                    current_info = self.provider.check_symbol(self.symbol)
                    tradability = str(current_info.get("tradability", "NONE"))
                    allowed = tradability == "BUY_SELL" or (
                        tradability == signal.side.value
                    )
                    if not allowed:
                        message = (
                            f"未下单：当前 tradability={tradability}，"
                            f"不允许 {signal.side.value}"
                        )
                        self._log("ERROR", message)
                        self._refresh_market_snapshot(message)
                        continue
                result = self.provider.place_order(
                    OrderRequest(
                        symbol=self.symbol,
                        side=signal.side,
                        reference_price=signal.price,
                        buy_notional=Decimal(self.config.app.buy_notional),
                        sell_quantity=Decimal(self.config.app.sell_quantity),
                    )
                )
                if result.accepted:
                    self.strategy.mark_executed(signal)
                    mode = "模拟" if result.paper else "实盘"
                    message = f"{mode}订单已接受: {result.order_id}；{result.message}"
                    self._log("ORDER", message)
                else:
                    message = f"订单被拒绝: {result.message}"
                    self._log("ERROR", message)
                self._refresh_market_snapshot(message)
        except Exception as exc:
            if not self.stop_event.is_set():
                message = str(exc) or exc.__class__.__name__
                self._log("ERROR", message)
                self._update(RunState.ERROR, message)
                return
        self._update(RunState.STOPPED, "已停止")
        self._log("INFO", "量化运行已停止")

    def _refresh_market_snapshot(self, message: str | None = None) -> None:
        warmup_bars = getattr(self.strategy, "warmup_bars", 0)
        warmup_required = getattr(self.strategy, "warmup_required", 0)
        direction = getattr(self.strategy, "direction", self._snapshot.direction)
        last_price = getattr(self.strategy, "last_price", None)
        ma_value = getattr(self.strategy, "ma_value", None)
        trades_today = getattr(self.strategy, "trades_today", 0)
        ready = warmup_bars >= warmup_required and direction.value != "UNKNOWN"
        state = RunState.RUNNING if ready else RunState.WARMING_UP
        if message is None:
            if ready:
                message = f"运行中；日线方向 {direction.value}"
            else:
                message = f"预热 {warmup_bars}/{warmup_required}，等待日线方向"
        with self._snapshot_lock:
            self._snapshot.state = state
            self._snapshot.direction = direction
            self._snapshot.last_price = last_price
            self._snapshot.ma_value = ma_value
            self._snapshot.warmup_bars = warmup_bars
            self._snapshot.warmup_required = warmup_required
            self._snapshot.trades_today = trades_today
            self._snapshot.message = message
            self._snapshot.updated_at = int(time.time() * 1000)
            snapshot = replace(self._snapshot)
        self.snapshot_callback(snapshot)

    def _on_provider_status(self, message: str) -> None:
        self._log("INFO", message)
        with self._snapshot_lock:
            self._snapshot.message = message
            self._snapshot.updated_at = int(time.time() * 1000)
            snapshot = replace(self._snapshot)
        self.snapshot_callback(snapshot)

    def _update(self, state: RunState, message: str) -> None:
        with self._snapshot_lock:
            self._snapshot.state = state
            self._snapshot.message = message
            self._snapshot.updated_at = int(time.time() * 1000)
            snapshot = replace(self._snapshot)
        self.snapshot_callback(snapshot)

    def _log(self, level: str, message: str) -> None:
        self.log_callback(level, self.symbol, message)


class TradingController:
    def __init__(
        self,
        snapshot_callback: SnapshotCallback,
        log_callback: LogCallback,
    ) -> None:
        self.snapshot_callback = snapshot_callback
        self.log_callback = log_callback
        self._runners: dict[str, SymbolRunner] = {}
        self._lock = threading.Lock()

    def start(self, symbol: str, config: RunnerConfig) -> None:
        symbol = symbol.upper()
        with self._lock:
            existing = self._runners.get(symbol)
            if existing and existing.is_alive:
                return
            runner = SymbolRunner(
                symbol,
                config,
                self.snapshot_callback,
                self.log_callback,
            )
            self._runners[symbol] = runner
            runner.start()

    def stop(self, symbol: str) -> None:
        with self._lock:
            runner = self._runners.get(symbol.upper())
        if runner:
            runner.stop()

    def stop_all(self) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.stop()

    def join_all(self, timeout_per_runner: float = 2.0) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.join(timeout=timeout_per_runner)

    def is_running(self, symbol: str) -> bool:
        with self._lock:
            runner = self._runners.get(symbol.upper())
            return bool(runner and runner.is_alive)

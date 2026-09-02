from __future__ import annotations

import threading
import time
from decimal import Decimal

from autoquant_backend.engine.config import LogCallback, RunnerConfig, SnapshotCallback
from autoquant_backend.engine.runner import SymbolRunner
from autoquant_backend.state import OrderLedger, PortfolioPerformance


class TradingController:
    def __init__(
        self,
        snapshot_callback: SnapshotCallback,
        log_callback: LogCallback,
        ledger: OrderLedger | None = None,
    ) -> None:
        self.snapshot_callback = snapshot_callback
        self.log_callback = log_callback
        self.ledger = ledger or OrderLedger()
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
                self.ledger,
            )
            self._runners[symbol] = runner
            runner.start()

    def stop(self, symbol: str, *, close_position: bool = False) -> None:
        with self._lock:
            runner = self._runners.get(symbol.upper())
        if runner:
            runner.stop(close_position=close_position)

    def stop_all(self, *, close_position: bool = False) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.stop(close_position=close_position)

    def stop_targets(
        self, symbols: list[str] | None = None
    ) -> list[tuple[str, str, Decimal]]:
        requested = (
            None if symbols is None else {symbol.upper() for symbol in symbols}
        )
        with self._lock:
            runners = [
                runner
                for symbol, runner in self._runners.items()
                if requested is None or symbol in requested
            ]
        targets: list[tuple[str, str, Decimal]] = []
        for runner in runners:
            mode = runner.config.app.trading_mode
            is_paper = mode != "REAL"
            quantity = self.ledger.position_summary(
                runner.symbol, paper=is_paper
            ).quantity
            has_blocking_order = bool(
                self.ledger.pending_count(runner.symbol, paper=is_paper)
                or self.ledger.unknown_count(runner.symbol, paper=is_paper)
            )
            if runner.is_alive or quantity != 0 or has_blocking_order:
                targets.append((runner.symbol, mode, quantity))
        return targets

    def join_all(self, timeout_per_runner: float = 2.0) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.join(timeout=timeout_per_runner)

    def wait_for_all(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            runner.join(timeout=remaining)
        return all(not runner.is_alive for runner in runners)

    def is_running(self, symbol: str) -> bool:
        with self._lock:
            runner = self._runners.get(symbol.upper())
            return bool(runner and runner.is_alive)

    def unknown_live_orders(self, symbol: str) -> int:
        return self.ledger.unknown_count(symbol.upper(), paper=False)

    def resolve_unknown_live_orders(self, symbol: str) -> int:
        if self.is_running(symbol):
            raise RuntimeError("请先停止该股票，再解除未知订单锁")
        return self.ledger.resolve_unknown(symbol.upper(), paper=False)

    def open_position_symbols(self, *, paper: bool) -> list[str]:
        return self.ledger.open_position_symbols(paper=paper)

    def portfolio_performance(
        self,
        *,
        paper: bool,
        market_prices: dict[str, Decimal],
    ) -> PortfolioPerformance:
        return self.ledger.portfolio_performance(
            paper=paper,
            market_prices=market_prices,
        )

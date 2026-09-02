from __future__ import annotations

import json
import threading
import time
from collections import deque
from decimal import Decimal
from pathlib import Path

from autoquant_backend.backtest import (
    BacktestService,
    BacktestStore,
    HistoricalArchiveService,
    HistoricalDownloader,
)
from autoquant_backend.engine import TradingController
from autoquant_backend.runtime.backtest_api import BacktestRuntimeMixin
from autoquant_backend.runtime.config_api import ConfigRuntimeMixin
from autoquant_backend.runtime.lifecycle import LifecycleRuntimeMixin
from autoquant_backend.runtime.models import ServiceLog
from autoquant_backend.runtime.payloads import snapshot_payload
from autoquant_backend.runtime.trading_api import TradingRuntimeMixin
from autoquant_backend.state import OrderLedger
from autoquant_shared.config import AppConfig, ConfigStore, default_config_path
from autoquant_shared.formatting import financial_text
from autoquant_shared.models import RuntimeSnapshot


class BackendRuntime(
    ConfigRuntimeMixin,
    TradingRuntimeMixin,
    BacktestRuntimeMixin,
    LifecycleRuntimeMixin,
):
    """Owns all long-running trading state independently of any frontend."""

    def __init__(
        self,
        *,
        config_store: ConfigStore | None = None,
        ledger: OrderLedger | None = None,
        desired_state_path: Path | None = None,
        log_capacity: int = 5000,
    ) -> None:
        self.config_store = config_store or ConfigStore()
        self.ledger = ledger or OrderLedger()
        self.backtest_store = BacktestStore(self.ledger.path)
        self.backtest_service = BacktestService(self.backtest_store)
        self.historical_archive_service = HistoricalArchiveService(
            self.backtest_store
        )
        self.desired_state_path = desired_state_path or default_config_path().with_name(
            "running.json"
        )
        self._lock = threading.RLock()
        self._config_lock = threading.RLock()
        self._futures_market_lock = threading.RLock()
        self._futures_market_cache: tuple[float, dict[str, Any]] | None = None
        self._account_provider_lock = threading.RLock()
        self._account_provider_key: tuple[Any, ...] | None = None
        self._account_provider: TradingProvider | None = None
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._logs: deque[ServiceLog] = deque(maxlen=max(100, log_capacity))
        self._log_sequence = 0
        runtime_config = self.config_store.load()
        self._paper_mode = runtime_config.trading_mode != "REAL"
        self.controller = TradingController(
            snapshot_callback=self._on_snapshot,
            log_callback=self._on_log,
            ledger=self.ledger,
        )
        self._sync_configured_snapshots(runtime_config)

    def _on_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.symbol] = snapshot_payload(snapshot)

    def _on_log(self, level: str, symbol: str, message: str) -> None:
        with self._lock:
            self._log_sequence += 1
            self._logs.append(
                ServiceLog(
                    sequence=self._log_sequence,
                    level=level,
                    symbol=symbol,
                    message=message,
                    created_at=int(time.time() * 1000),
                )
            )

    def status(self, after_log: int = 0) -> dict[str, Any]:
        with self._lock:
            logs = [
                _json_value(asdict(item))
                for item in self._logs
                if item.sequence > max(0, after_log)
            ]
            snapshots = [dict(payload) for payload in self._snapshots.values()]
            sequence = self._log_sequence
        symbols = [str(payload.get("symbol", "")).upper() for payload in snapshots]
        market_prices: dict[str, Decimal] = {}
        for payload in snapshots:
            symbol = str(payload.get("symbol", "")).upper()
            try:
                last_price = Decimal(str(payload.get("last_price")))
                if symbol and last_price.is_finite() and last_price > 0:
                    market_prices[symbol] = last_price
            except (ArithmeticError, ValueError):
                pass
        performances = self.ledger.symbol_performances(
            paper=self._paper_mode,
            market_prices=market_prices,
            symbols=symbols,
        )
        for payload in snapshots:
            symbol = str(payload.get("symbol", "")).upper()
            performance = performances.get(symbol)
            if performance is None:
                continue
            payload["realized_pnl"] = financial_text(performance.realized_pnl)
            payload["unrealized_pnl"] = (
                None
                if performance.unrealized_pnl is None
                else financial_text(performance.unrealized_pnl)
            )
            payload["profit"] = (
                None
                if performance.unrealized_pnl is None
                else financial_text(
                    performance.realized_pnl + performance.unrealized_pnl
                )
            )
        return {
            "snapshots": snapshots,
            "logs": logs,
            "last_log_sequence": sequence,
            "server_time": int(time.time() * 1000),
        }

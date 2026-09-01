from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from autoquant_shared.config import (
    AppConfig,
    ConfigStore,
    SECRET_SENTINEL,
    credential_or_environment,
    default_config_path,
)
from autoquant_backend.engine import RunnerConfig, TradingController, create_provider
from autoquant_backend.providers.base import TradingProvider
from autoquant_backend.providers.binance_futures import BinanceFuturesProvider
from autoquant_backend.backtest import (
    BacktestService,
    BacktestStore,
    HistoricalArchiveService,
    HistoricalDownloader,
)
from autoquant_shared.models import AccountOverview, Direction, RuntimeSnapshot
from autoquant_backend.state import OrderLedger
from autoquant_shared.formatting import financial_text


FUTURES_MARKET_CACHE_SECONDS = 60.0


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def snapshot_payload(snapshot: RuntimeSnapshot) -> dict[str, Any]:
    payload = _json_value(asdict(snapshot))
    for field_name in (
        "last_price",
        "ma_value",
        "average_entry_price",
        "session_open_notional",
        "realized_pnl",
        "unrealized_pnl",
        "profit",
    ):
        value = getattr(snapshot, field_name)
        payload[field_name] = None if value is None else financial_text(value)
    return payload


def overview_payload(overview: AccountOverview) -> dict[str, Any]:
    payload = _json_value(asdict(overview))
    for field_name in ("total_balance", "realized_pnl", "unrealized_pnl"):
        value = getattr(overview, field_name)
        payload[field_name] = None if value is None else financial_text(value)
    return payload


@dataclass(frozen=True, slots=True)
class ServiceLog:
    sequence: int
    level: str
    symbol: str
    message: str
    created_at: int


class BackendRuntime:
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

    def public_config(self) -> dict[str, Any]:
        with self._config_lock:
            config = self.config_store.load()
            payload = asdict(config)
            payload["api_key"] = SECRET_SENTINEL if self._api_key(config) else ""
            payload["api_secret"] = (
                SECRET_SENTINEL if self._api_secret(config) else ""
            )
            payload["openai_api_key"] = (
                SECRET_SENTINEL if self._openai_api_key(config) else ""
            )
            payload["deepseek_api_key"] = (
                SECRET_SENTINEL if self._deepseek_api_key(config) else ""
            )
            payload["qwen_api_key"] = (
                SECRET_SENTINEL if self._qwen_api_key(config) else ""
            )
            return payload

    def futures_rankings(self, limit: int = 20) -> dict[str, Any]:
        requested_limit = int(limit)
        if not 1 <= requested_limit <= 100:
            raise ValueError("涨跌榜数量必须在 1 到 100 之间")
        with self._futures_market_lock:
            cached = self._futures_market_cache
            now = time.monotonic()
            if (
                cached is None
                or now - cached[0] >= FUTURES_MARKET_CACHE_SECONDS
            ):
                try:
                    payload = BinanceFuturesProvider(
                        include_daily_stream=False
                    ).get_24h_rankings(100)
                except Exception:
                    if cached is None:
                        raise
                    payload = cached[1]
                else:
                    self._futures_market_cache = (time.monotonic(), payload)
            else:
                payload = cached[1]

            ranking_names = (
                "stock_gainers",
                "stock_losers",
                "crypto_gainers",
                "crypto_losers",
            )
            rankings = {
                name: payload.get(name, []) for name in ranking_names
            }
            tickers = payload.get("tickers", {})
            if any(not isinstance(items, list) for items in rankings.values()):
                raise RuntimeError("Futures 行情缓存格式不正确")
            if not isinstance(tickers, dict):
                raise RuntimeError("Futures 行情缓存格式不正确")
            return {
                **{
                    name: [
                        dict(item)
                        for item in items[:requested_limit]
                        if isinstance(item, dict)
                    ]
                    for name, items in rankings.items()
                },
                "tickers": {
                    str(symbol): dict(item)
                    for symbol, item in tickers.items()
                    if isinstance(item, dict)
                },
                "updated_at": int(payload.get("updated_at", 0)),
                "window": str(payload.get("window", "24h")),
            }

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._config_lock:
            payload = dict(payload)
            if (
                "max_trades_per_day" in payload
                and "max_additions_per_position" not in payload
            ):
                payload["max_additions_per_position"] = payload.pop(
                    "max_trades_per_day"
                )
            current = self.config_store.load()
            allowed = set(asdict(current))
            unknown = sorted(set(payload) - allowed)
            if unknown:
                raise ValueError("未知配置字段: " + ", ".join(unknown))
            merged = asdict(current)
            merged.update(payload)
            if payload.get("api_key") == SECRET_SENTINEL:
                merged["api_key"] = current.api_key
            if payload.get("api_secret") == SECRET_SENTINEL:
                merged["api_secret"] = current.api_secret
            if payload.get("openai_api_key") == SECRET_SENTINEL:
                merged["openai_api_key"] = current.openai_api_key
            if payload.get("deepseek_api_key") == SECRET_SENTINEL:
                merged["deepseek_api_key"] = current.deepseek_api_key
            if payload.get("qwen_api_key") == SECRET_SENTINEL:
                merged["qwen_api_key"] = current.qwen_api_key
            config = AppConfig(**merged)
            config.validate()
            self.config_store.save(config)
            self._paper_mode = config.trading_mode != "REAL"
            with self._account_provider_lock:
                self._account_provider_key = None
                self._account_provider = None
            self._sync_configured_snapshots(config)
            return self.public_config()

    def _sync_configured_snapshots(self, config: AppConfig) -> None:
        paper = config.trading_mode != "REAL"
        with self._lock:
            synchronized: dict[str, dict[str, Any]] = {}
            for symbol in config.symbols:
                payload = dict(
                    self._snapshots.get(
                        symbol,
                        snapshot_payload(
                            RuntimeSnapshot(
                                symbol=symbol,
                                direction=Direction.FLAT,
                            )
                        ),
                    )
                )
                market_prices: dict[str, Decimal] = {}
                try:
                    last_price = Decimal(str(payload.get("last_price")))
                    if last_price.is_finite() and last_price > 0:
                        market_prices[symbol] = last_price
                except (ArithmeticError, ValueError):
                    pass
                performance = self.ledger.portfolio_performance(
                    paper=paper,
                    market_prices=market_prices,
                    symbol=symbol,
                )
                payload["profit"] = (
                    None
                    if performance.unrealized_pnl is None
                    else financial_text(
                        performance.realized_pnl + performance.unrealized_pnl
                    )
                )
                payload["realized_pnl"] = financial_text(
                    performance.realized_pnl
                )
                payload["unrealized_pnl"] = (
                    None
                    if performance.unrealized_pnl is None
                    else financial_text(performance.unrealized_pnl)
                )
                synchronized[symbol] = payload
            self._snapshots = synchronized

    @staticmethod
    def _api_key(config: AppConfig) -> str:
        return credential_or_environment(config.api_key, "BINANCE_API_KEY")

    @staticmethod
    def _api_secret(config: AppConfig) -> str:
        return credential_or_environment(config.api_secret, "BINANCE_API_SECRET")

    @staticmethod
    def _openai_api_key(config: AppConfig) -> str:
        return credential_or_environment(
            config.openai_api_key, "OPENAI_API_KEY"
        )

    @staticmethod
    def _deepseek_api_key(config: AppConfig) -> str:
        return credential_or_environment(
            config.deepseek_api_key, "DEEPSEEK_API_KEY"
        )

    @staticmethod
    def _qwen_api_key(config: AppConfig) -> str:
        return credential_or_environment(
            config.qwen_api_key, "DASHSCOPE_API_KEY"
        )

    def _runner_config(
        self,
        manual_direction: Direction = Direction.FLAT,
        *,
        openai_api_key: str = "",
        deepseek_api_key: str = "",
        qwen_api_key: str = "",
    ) -> RunnerConfig:
        config = self.config_store.load()
        transient_openai_key = openai_api_key.strip()
        transient_deepseek_key = deepseek_api_key.strip()
        transient_qwen_key = qwen_api_key.strip()
        return RunnerConfig(
            app=config,
            api_key=self._api_key(config),
            api_secret=self._api_secret(config),
            openai_api_key=(
                transient_openai_key
                if transient_openai_key != SECRET_SENTINEL
                else ""
            )
            or self._openai_api_key(config),
            deepseek_api_key=(
                transient_deepseek_key
                if transient_deepseek_key != SECRET_SENTINEL
                else ""
            )
            or self._deepseek_api_key(config),
            qwen_api_key=(
                transient_qwen_key
                if transient_qwen_key != SECRET_SENTINEL
                else ""
            )
            or self._qwen_api_key(config),
            manual_direction=manual_direction,
        )

    def start(
        self,
        symbol: str,
        direction: str,
        *,
        openai_api_key: str = "",
        deepseek_api_key: str = "",
        qwen_api_key: str = "",
    ) -> None:
        config = self.config_store.load()
        manual_direction = Direction(direction.strip().upper())
        if config.ai_provider == "DISABLED":
            if manual_direction is Direction.UNKNOWN:
                raise ValueError("大模型禁用时必须选择手动开仓方向")
        else:
            manual_direction = Direction.UNKNOWN
        normalized = symbol.strip().upper()
        if normalized not in config.symbols:
            raise ValueError(f"股票 {normalized} 不在服务器配置中")
        runner_config = self._runner_config(
            manual_direction,
            openai_api_key=openai_api_key,
            deepseek_api_key=deepseek_api_key,
            qwen_api_key=qwen_api_key,
        )
        if config.ai_provider in {"CHATGPT", "DUAL"} and not (
            runner_config.openai_api_key
        ):
            raise ValueError("已启用 ChatGPT 决策，但未提供 OpenAI API Key")
        if config.ai_provider in {"DEEPSEEK", "DUAL"} and not (
            runner_config.deepseek_api_key
        ):
            raise ValueError("已启用 DeepSeek 决策，但未提供 DeepSeek API Key")
        if config.ai_provider == "QWEN" and not runner_config.qwen_api_key:
            raise ValueError("已启用 Qwen 决策，但未提供 Qwen API Key")
        self.controller.start(normalized, runner_config)
        self._set_desired(normalized, manual_direction, running=True)

    def stop(self, symbol: str, *, close_position: bool) -> None:
        normalized = symbol.strip().upper()
        self.controller.stop(normalized, close_position=close_position)
        self._set_desired(normalized, Direction.FLAT, running=False)

    def stop_targets(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        return [
            {"symbol": symbol, "mode": mode, "quantity": format(quantity, "f")}
            for symbol, mode, quantity in self.controller.stop_targets(symbols)
        ]

    def unknown_live_orders(self, symbol: str) -> int:
        return self.controller.unknown_live_orders(symbol)

    def resolve_unknown_live_orders(self, symbol: str) -> int:
        return self.controller.resolve_unknown_live_orders(symbol)

    def check_connection(self, symbol: str) -> dict[str, Any]:
        provider = create_provider(self._runner_config())
        info = provider.check_symbol(symbol.strip().upper())
        validation = str(info.get("validation", ""))
        message = validation or (
            f"连接成功；{symbol.strip().upper()} "
            f"tradability={info.get('tradability', 'UNKNOWN')}"
        )
        return {"message": message, "info": _json_value(info)}

    def account_overview(self, market_prices: dict[str, Any]) -> dict[str, Any]:
        runner_config = self._runner_config()
        paper = runner_config.app.trading_mode != "REAL"
        provider = self._shared_account_provider(runner_config)
        account_currency = provider.quote_asset
        prices: dict[str, Decimal] = {}
        for symbol, value in market_prices.items():
            try:
                parsed = Decimal(str(value))
            except (ArithmeticError, ValueError):
                continue
            if parsed.is_finite() and parsed > 0:
                prices[symbol.upper()] = parsed

        open_symbols = self.controller.open_position_symbols(paper=paper)
        now_ms = int(time.time() * 1000)
        with self._lock:
            snapshots = {
                symbol: dict(self._snapshots.get(symbol, {}))
                for symbol in open_symbols
            }
        for symbol, snapshot in snapshots.items():
            try:
                updated_at = int(snapshot.get("updated_at", 0))
                latest = Decimal(str(snapshot.get("last_price")))
            except (ArithmeticError, TypeError, ValueError):
                continue
            if (
                updated_at > 0
                and now_ms - updated_at <= 120_000
                and latest.is_finite()
                and latest > 0
            ):
                prices[symbol] = latest

        total_balance: Decimal | None = None
        errors: list[str] = []
        if runner_config.api_key and runner_config.api_secret:
            try:
                total_balance = provider.get_account_total(account_currency)
            except Exception as exc:
                errors.append(f"账户总金额不可用：{exc}")
            for symbol in open_symbols:
                if symbol in prices:
                    continue
                try:
                    prices[symbol] = provider.get_latest_price(symbol)
                except Exception as exc:
                    prices.pop(symbol, None)
                    errors.append(f"{symbol} 报价不可用：{exc}")
        else:
            errors.append("服务器未配置 Binance API Key 和 Secret")

        performance = self.controller.portfolio_performance(
            paper=paper, market_prices=prices
        )
        if errors:
            message = "；".join(errors)
        elif runner_config.app.provider == "binance_futures":
            message = (
                f"账户总金额来自 Binance Futures {account_currency} 余额；"
                f"盈亏仅统计本程序{'模拟' if paper else '实盘'}订单"
            )
        else:
            message = (
                "账户总金额来自 Binance 全部激活钱包的 USDC 折算；"
                f"盈亏仅统计本程序{'模拟' if paper else '实盘'}订单"
            )
        return overview_payload(
            AccountOverview(
                total_balance=total_balance,
                realized_pnl=performance.realized_pnl,
                unrealized_pnl=performance.unrealized_pnl,
                currency=account_currency,
                missing_price_symbols=performance.missing_price_symbols,
                message=message,
                updated_at=int(time.time() * 1000),
            )
        )

    def _shared_account_provider(
        self, runner_config: RunnerConfig
    ) -> TradingProvider:
        app = runner_config.app
        key = (
            app.provider,
            app.trading_mode,
            app.rest_base_url,
            app.websocket_base_url,
            app.recv_window,
            app.leverage,
            runner_config.api_key,
            runner_config.api_secret,
        )
        with self._account_provider_lock:
            if self._account_provider is None or self._account_provider_key != key:
                self._account_provider = create_provider(runner_config)
                self._account_provider_key = key
            return self._account_provider

    def trade_history(
        self,
        *,
        symbol: str = "",
        action: str = "ALL",
        paper: bool | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        items = self.ledger.trade_history(
            symbol=symbol,
            action=action,
            paper=paper,
            limit=limit,
        )
        return {
            "items": [
                {
                    "executed_at": item.executed_at,
                    "symbol": item.symbol,
                    "action": item.action,
                    "opening_direction": item.opening_direction,
                    "price": financial_text(item.price),
                    "quantity": format(item.quantity, "f"),
                    "amount": financial_text(item.amount),
                    "fee": financial_text(item.fee),
                    "profit": financial_text(item.profit),
                    "paper": item.paper,
                }
                for item in items
            ],
            "count": len(items),
        }

    def ai_decision_history(
        self,
        *,
        symbol: str = "",
        stage: str = "ALL",
        limit: int = 100,
    ) -> dict[str, Any]:
        items = self.ledger.ai_decision_history(
            symbol=symbol,
            stage=stage,
            limit=limit,
        )
        return {
            "items": [
                {
                    "record_id": item.record_id,
                    "decided_at": item.decided_at,
                    "symbol": item.symbol,
                    "stage": item.stage,
                    "provider": item.provider,
                    "model": item.model,
                    "outcome": item.outcome,
                    "confidence": item.confidence,
                    "summary": item.summary,
                    "factors": list(item.factors),
                    "risks": list(item.risks),
                    "input_json": item.input_json,
                    "output_json": item.output_json,
                    "fallback": item.fallback,
                    "elapsed_ms": item.elapsed_ms,
                    "response_ms": item.response_ms,
                }
                for item in items
            ],
            "count": len(items),
        }

    def start_historical_download(self, symbol: str) -> dict[str, Any]:
        runner_config = self._runner_config()
        provider_name = runner_config.app.provider
        downloader = HistoricalDownloader(
            self.backtest_store,
            lambda: create_provider(runner_config),
            provider_name,
        )
        download_id = downloader.start(symbol)
        return {"accepted": True, "download_id": download_id}

    def historical_downloads(self, limit: int = 50) -> dict[str, Any]:
        items = self.backtest_store.list_downloads(limit)
        return {"items": items, "count": len(items)}

    def export_historical_bars(
        self, symbol: str, provider: str = ""
    ) -> tuple[bytes, str]:
        selected_provider = provider.strip().lower() or self.config_store.load().provider
        return self.historical_archive_service.export(
            selected_provider, symbol
        )

    def import_historical_bars(
        self, payload: bytes, *, expected_symbol: str = ""
    ) -> dict[str, Any]:
        return self.historical_archive_service.import_archive(
            payload, expected_symbol=expected_symbol
        )

    def delete_historical_bars(
        self, symbol: str, provider: str = ""
    ) -> dict[str, Any]:
        selected_provider = provider.strip().lower() or self.config_store.load().provider
        result = self.backtest_store.delete_historical_bars(
            selected_provider, symbol
        )
        return {
            "provider": selected_provider,
            "symbol": symbol.strip().upper(),
            **result,
        }

    def start_backtest(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.config_store.load()
        strategy = str(payload.get("strategy", config.strategy)).strip()
        symbol = str(payload.get("symbol", "")).strip().upper()
        run_id = self.backtest_service.start(
            config.provider, symbol, strategy, config
        )
        return {"accepted": True, "run_id": run_id}

    def backtest_runs(self, limit: int = 100) -> dict[str, Any]:
        items = self.backtest_store.list_runs(limit)
        for item in items:
            for field in (
                "total_pnl",
                "return_percent",
                "max_drawdown_percent",
            ):
                item[field] = financial_text(Decimal(str(item.get(field, "0"))))
        return {"items": items, "count": len(items)}

    def backtest_trade_details(
        self, run_id: str, limit: int = 50_000
    ) -> dict[str, Any]:
        items = self.backtest_store.backtest_trades(run_id, limit)
        for item in items:
            for field in (
                "entry_price",
                "exit_price",
                "quantity",
                "pnl",
            ):
                item[field] = financial_text(Decimal(str(item.get(field, "0"))))
        return {"items": items, "count": len(items)}

    def restore_desired_runners(self) -> list[str]:
        if not self.desired_state_path.exists():
            return []
        try:
            payload = json.loads(self.desired_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        config = self.config_store.load()
        if config.trading_mode == "REAL" and os.environ.get(
            "AUTOQUANT_RESTORE_REAL", ""
        ).strip() != "1":
            self._on_log(
                "ERROR",
                "SYSTEM",
                "检测到 REAL 模式的恢复记录；未设置 AUTOQUANT_RESTORE_REAL=1，"
                "为安全起见没有自动重启实盘策略",
            )
            return []
        restored: list[str] = []
        for symbol, direction in payload.get("runners", {}).items():
            try:
                self.start(symbol, direction)
                restored.append(symbol)
            except Exception as exc:
                self._on_log("ERROR", symbol, f"恢复运行失败：{exc}")
        return restored

    def _set_desired(
        self, symbol: str, direction: Direction, *, running: bool
    ) -> None:
        with self._lock:
            payload: dict[str, Any] = {"runners": {}}
            if self.desired_state_path.exists():
                try:
                    loaded = json.loads(
                        self.desired_state_path.read_text(encoding="utf-8")
                    )
                    if isinstance(loaded, dict) and isinstance(
                        loaded.get("runners"), dict
                    ):
                        payload = loaded
                except (OSError, json.JSONDecodeError):
                    pass
            runners = payload.setdefault("runners", {})
            if running:
                runners[symbol] = direction.value
            else:
                runners.pop(symbol, None)
            self.desired_state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.desired_state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.desired_state_path)

    def shutdown(self, timeout: float = 10.0) -> None:
        # Do not close positions on service shutdown. Desired runners remain on
        # disk so a supervised restart can restore them safely.
        self.controller.stop_all(close_position=False)
        self.controller.wait_for_all(timeout=timeout)

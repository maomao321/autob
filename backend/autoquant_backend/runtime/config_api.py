from __future__ import annotations

import os
from dataclasses import asdict, replace
from decimal import Decimal
from typing import Any

from autoquant_backend.engine import RunnerConfig
from autoquant_backend.runtime.payloads import snapshot_payload
from autoquant_shared.config import (
    AppConfig,
    SECRET_SENTINEL,
    STRATEGY_CONFIG_FIELDS,
)
from autoquant_shared.formatting import financial_text
from autoquant_shared.models import Direction, RuntimeSnapshot


class ConfigRuntimeMixin:
    """Configuration API and runner configuration assembly."""
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

    def _credential(self, configured_value: str, environment_name: str) -> str:
        configured = str(configured_value).strip()
        if configured or not self.allow_environment_credentials:
            return configured
        return os.environ.get(environment_name, "").strip()

    def _api_key(self, config: AppConfig) -> str:
        return self._credential(config.api_key, "BINANCE_API_KEY")

    def _api_secret(self, config: AppConfig) -> str:
        return self._credential(config.api_secret, "BINANCE_API_SECRET")

    def _openai_api_key(self, config: AppConfig) -> str:
        return self._credential(
            config.openai_api_key, "OPENAI_API_KEY"
        )

    def _deepseek_api_key(self, config: AppConfig) -> str:
        return self._credential(
            config.deepseek_api_key, "DEEPSEEK_API_KEY"
        )

    def _qwen_api_key(self, config: AppConfig) -> str:
        return self._credential(
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

from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal
from typing import Any

from autoquant_backend.backtest import HistoricalDownloader
from autoquant_backend.engine import create_provider
from autoquant_shared.config import (
    AppConfig,
    STRATEGY_CONFIG_FIELDS,
    strategy_config_snapshot,
)
from autoquant_shared.formatting import financial_text


class BacktestRuntimeMixin:
    """Historical data and backtest APIs."""
    def start_historical_download(
        self, symbol: str, provider: str = ""
    ) -> dict[str, Any]:
        runner_config = self._runner_config()
        provider_name = provider.strip().lower() or runner_config.app.provider
        if provider_name != runner_config.app.provider:
            runner_config = replace(
                runner_config,
                app=replace(runner_config.app, provider=provider_name),
            )
        downloader = HistoricalDownloader(
            self.backtest_store,
            lambda: create_provider(runner_config),
            provider_name,
        )
        download_id = downloader.start(symbol)
        return {"accepted": True, "download_id": download_id}

    def update_historical_download(self, download_id: str) -> dict[str, Any]:
        download = self.backtest_store.get_download(download_id)
        if download is None:
            raise ValueError("历史 K 线下载记录不存在")
        result = self.start_historical_download(
            str(download.get("symbol", "")),
            str(download.get("provider", "")),
        )
        return {
            **result,
            "source_download_id": download_id.strip(),
            "update": True,
        }

    def historical_downloads(self, limit: int = 50) -> dict[str, Any]:
        items = self.backtest_store.list_downloads(limit)
        return {"items": items, "count": len(items)}

    def wait_backtest_status(
        self, after_revision: int = -1, timeout: float = 10.0
    ) -> dict[str, Any]:
        revision = self.backtest_store.wait_for_status_change(
            after_revision, timeout
        )
        changed = revision != after_revision
        if not changed:
            return {"revision": revision, "changed": False}
        downloads = self.historical_downloads()["items"]
        runs = self.backtest_runs()["items"]
        return {
            "revision": revision,
            "changed": True,
            "downloads": downloads,
            "runs": runs,
        }

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
        strategy_payload = payload.get("strategy_config", {})
        if not isinstance(strategy_payload, dict):
            raise ValueError("策略配置副本格式不正确")
        allowed_fields = set(STRATEGY_CONFIG_FIELDS.get(strategy, ()))
        unknown_fields = sorted(set(strategy_payload) - allowed_fields)
        if unknown_fields:
            raise ValueError(
                "未知策略配置字段: " + ", ".join(unknown_fields)
            )
        merged = asdict(config)
        merged.update(
            {
                field_name: strategy_payload[field_name]
                for field_name in allowed_fields
                if field_name in strategy_payload
            }
        )
        merged["strategy"] = strategy
        merged["provider"] = (
            str(payload.get("provider", "")).strip().lower()
            or config.provider
        )
        run_config = AppConfig(**merged)
        run_config.validate()
        run_id = self.backtest_service.start(
            run_config.provider,
            symbol,
            strategy,
            run_config,
            download_id=str(payload.get("download_id", "")),
        )
        return {"accepted": True, "run_id": run_id}

    def stop_backtest(self, run_id: str) -> dict[str, Any]:
        self.backtest_service.cancel(run_id)
        return {"accepted": True, "run_id": run_id.strip()}

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

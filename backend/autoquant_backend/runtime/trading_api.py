from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Any

from autoquant_backend.engine import create_provider
from autoquant_backend.providers.base import TradingProvider
from autoquant_backend.providers.binance_futures import BinanceFuturesProvider
from autoquant_backend.runtime.constants import FUTURES_MARKET_CACHE_SECONDS
from autoquant_backend.runtime.payloads import _json_value, overview_payload
from autoquant_shared.formatting import financial_text
from autoquant_shared.models import AccountOverview, Direction, RuntimeSnapshot


class TradingRuntimeMixin:
    """Market, runner, account, trade history, and AI history APIs."""
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

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from autoquant_backend.ai_decision import (
    DecisionClient,
    DeepSeekDecisionClient,
    OpenAIResponsesDecisionClient,
    OpeningDecisionService,
    PublicMarketContextCollector,
    QwenDecisionClient,
)
from autoquant_backend.engine.config import OpeningDecider, RunnerConfig
from autoquant_backend.providers.base import TradingProvider
from autoquant_backend.providers.binance_futures import BinanceFuturesProvider
from autoquant_backend.providers.binance_stocks import BinanceStocksProvider
from autoquant_backend.strategies.base import Strategy
from autoquant_backend.strategies.five_minute_breakout import FiveMinuteBreakoutStrategy
from autoquant_shared.models import Direction


def create_provider(config: RunnerConfig) -> TradingProvider:
    if config.app.provider == "binance_stocks":
        return BinanceStocksProvider(
            api_key=config.api_key,
            api_secret=config.api_secret,
            live_trading=config.app.trading_mode == "REAL",
            rest_base_url=config.app.rest_base_url,
            websocket_base_url=config.app.websocket_base_url,
            recv_window=config.app.recv_window,
            include_daily_stream=config.manual_direction is Direction.UNKNOWN,
        )
    if config.app.provider == "binance_futures":
        return BinanceFuturesProvider(
            api_key=config.api_key,
            api_secret=config.api_secret,
            live_trading=config.app.trading_mode == "REAL",
            leverage=config.app.leverage,
            recv_window=config.app.recv_window,
            include_daily_stream=config.manual_direction is Direction.UNKNOWN,
        )
    raise ValueError(f"未知 API 供应商: {config.app.provider}")


def create_strategy(symbol: str, config: RunnerConfig) -> Strategy:
    if config.app.strategy == "five_minute_breakout":
        return FiveMinuteBreakoutStrategy(
            symbol=symbol,
            entry_context_bars=config.app.ai_entry_timing_bars,
            manual_direction=(
                None
                if config.manual_direction is Direction.UNKNOWN
                else config.manual_direction
            ),
        )
    raise ValueError(f"未知策略: {config.app.strategy}")


def create_opening_decider(
    config: RunnerConfig,
    model_log_callback: Callable[[str], None] | None = None,
    model_input_capture_callback: Callable[
        [str, str, str, dict[str, Any]], None
    ]
    | None = None,
    model_output_capture_callback: Callable[
        [str, str, str, dict[str, Any], int], None
    ]
    | None = None,
    market_data_provider: TradingProvider | None = None,
) -> OpeningDecider | None:
    mode = config.app.ai_provider
    if mode == "DISABLED":
        return None
    clients: list[DecisionClient] = []
    if mode in {"CHATGPT", "DUAL"}:
        if not config.openai_api_key.strip():
            raise ValueError("CHATGPT/DUAL 模式必须填写 OpenAI API Key")
        clients.append(
            OpenAIResponsesDecisionClient(
                api_key=config.openai_api_key,
                model=config.app.openai_model,
                timeout_seconds=config.app.ai_timeout_seconds,
                reasoning_enabled=config.app.openai_reasoning_enabled,
                reasoning_effort=config.app.openai_reasoning_effort,
                output_log_callback=model_log_callback,
                output_capture_callback=model_output_capture_callback,
            )
        )
    if mode in {"DEEPSEEK", "DUAL"}:
        if not config.deepseek_api_key.strip():
            raise ValueError("DEEPSEEK/DUAL 模式必须填写 DeepSeek API Key")
        clients.append(
            DeepSeekDecisionClient(
                api_key=config.deepseek_api_key,
                model=config.app.deepseek_model,
                timeout_seconds=config.app.ai_timeout_seconds,
                thinking_enabled=config.app.deepseek_thinking_enabled,
                reasoning_effort=config.app.deepseek_reasoning_effort,
                output_log_callback=model_log_callback,
                output_capture_callback=model_output_capture_callback,
            )
        )
    if mode == "QWEN":
        if not config.qwen_api_key.strip():
            raise ValueError("QWEN 模式必须填写 Qwen API Key")
        clients.append(
            QwenDecisionClient(
                api_key=config.qwen_api_key,
                model=config.app.qwen_model,
                chat_url=config.app.qwen_chat_url,
                timeout_seconds=config.app.ai_timeout_seconds,
                thinking_enabled=config.app.qwen_thinking_enabled,
                reasoning_effort=config.app.qwen_reasoning_effort,
                output_log_callback=model_log_callback,
                output_capture_callback=model_output_capture_callback,
            )
        )
    historical_bars_fetcher = (
        getattr(market_data_provider, "get_historical_bars", None)
        if market_data_provider is not None
        else None
    )

    def resolve_historical_symbol(symbol: str) -> str:
        normalized = symbol.strip().upper()
        if (
            market_data_provider is not None
            and market_data_provider.name == "binance_futures"
        ):
            quote_asset = market_data_provider.quote_asset.strip().upper()
            if quote_asset and not normalized.endswith(quote_asset):
                return normalized + quote_asset
        return normalized

    collector = PublicMarketContextCollector(
        history_days=config.app.ai_history_days,
        news_days=config.app.ai_news_days,
        news_limit=config.app.ai_news_limit,
        timeout_seconds=config.app.ai_timeout_seconds,
        historical_bars_fetcher=(
            historical_bars_fetcher
            if callable(historical_bars_fetcher)
            else None
        ),
        historical_source_name=(
            f"{market_data_provider.name} API"
            if market_data_provider is not None
            else ""
        ),
        historical_symbol_resolver=(
            resolve_historical_symbol
            if market_data_provider is not None
            else None
        ),
    )
    return OpeningDecisionService(
        collector=collector,
        clients=tuple(clients),
        min_confidence=float(Decimal(config.app.ai_min_confidence)),
        mode=mode,
        entry_timing_bar_count=config.app.ai_entry_timing_bars,
        input_capture_callback=model_input_capture_callback,
    )



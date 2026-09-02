from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from autoquant_backend.ai_decision.constants import ENTRY_TIMING_BAR_COUNT
from autoquant_backend.ai_decision.context import _bar_payload
from autoquant_backend.ai_decision.models import (
    DecisionClient,
    EntryTimingDecision,
    MarketContextCollector,
    ModelInputCapture,
    OpeningDecision,
)
from autoquant_backend.ai_decision.sanitizing import (
    _clean_text,
    _safe_error,
    _safe_structured_context,
)
from autoquant_shared.formatting import financial_text
from autoquant_shared.models import Bar, Direction, Signal


class OpeningDecisionService:
    def __init__(
        self,
        collector: MarketContextCollector,
        clients: tuple[DecisionClient, ...],
        min_confidence: float,
        mode: str,
        entry_timing_bar_count: int = ENTRY_TIMING_BAR_COUNT,
        input_capture_callback: ModelInputCapture | None = None,
    ) -> None:
        if not clients:
            raise ValueError("至少需要一个大模型客户端")
        self.collector = collector
        self.clients = clients
        self.min_confidence = min_confidence
        self.mode = mode.upper()
        self.entry_timing_bar_count = int(entry_timing_bar_count)
        self.input_capture_callback = input_capture_callback
        if not 10 <= self.entry_timing_bar_count <= 300:
            raise ValueError("开仓时机五分钟 K 线数量必须在 10 到 300 之间")
        self._context_lock = threading.Lock()
        self._daily_contexts: dict[str, tuple[int, int, dict[str, Any]]] = {}
        self._market_data_symbols: dict[str, str] = {}

    def set_market_data_symbol(
        self, trading_symbol: str, market_data_symbol: str
    ) -> None:
        trading_symbol = trading_symbol.strip().upper()
        market_data_symbol = market_data_symbol.strip().upper()
        if not trading_symbol or not market_data_symbol:
            return
        with self._context_lock:
            self._market_data_symbols[trading_symbol] = market_data_symbol
        set_trading_symbol = getattr(
            self.collector, "set_trading_symbol", None
        )
        if callable(set_trading_symbol):
            set_trading_symbol(market_data_symbol, trading_symbol)

    def decide(self, symbol: str, current_daily_bar: Bar) -> OpeningDecision:
        trading_symbol = symbol.upper()
        provider_label = self.mode
        model_label = "+".join(client.model for client in self.clients)
        with self._context_lock:
            market_data_symbol = self._market_data_symbols.get(
                trading_symbol, trading_symbol
            )
        try:
            context = self.collector.collect(
                market_data_symbol, current_daily_bar
            )
        except Exception as exc:
            return OpeningDecision.flat(
                f"市场上下文获取失败：{_safe_error(exc)}",
                provider=provider_label,
                model=model_label,
                risks=("新闻或走势数据不可用，已禁止当日新开仓",),
            )
        context = dict(context)
        context["symbol"] = trading_symbol
        if market_data_symbol != trading_symbol:
            context["market_data_symbol"] = market_data_symbol
        # The service owns this field so every collector implementation gives
        # both decision stages the same complete daily OHLC contract.
        context["current_session"] = _bar_payload(current_daily_bar)
        with self._context_lock:
            self._daily_contexts[trading_symbol] = (
                current_daily_bar.open_time,
                current_daily_bar.close_time,
                context,
            )
        news = context.get("recent_news")
        if not isinstance(news, list) or not news:
            return OpeningDecision.flat(
                "未获取到可用的近期新闻，无法完成多因素判断",
                provider=provider_label,
                model=model_label,
                risks=("近期新闻数据缺失，已禁止当日新开仓",),
            )

        self._capture_model_input("今日方向", context)
        if len(self.clients) == 1:
            try:
                decision = self.clients[0].decide(context)
            except Exception as exc:
                return OpeningDecision.flat(
                    f"大模型决策失败：{_safe_error(exc)}",
                    provider=provider_label,
                    model=model_label,
                    risks=("模型调用失败，已禁止当日新开仓",),
                )
            return self._enforce_confidence(decision)

        decisions: list[OpeningDecision] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=len(self.clients)) as executor:
            futures = {
                executor.submit(client.decide, context): client
                for client in self.clients
            }
            for future in as_completed(futures):
                client = futures[future]
                try:
                    decisions.append(future.result())
                except Exception as exc:
                    failures.append(f"{client.provider}: {_safe_error(exc)}")
        if failures or len(decisions) != len(self.clients):
            return OpeningDecision.flat(
                "双模型未能全部完成决策：" + "；".join(failures),
                provider=provider_label,
                model=model_label,
                risks=("任一模型失败时，双模型模式禁止当日新开仓",),
            )

        checked = [self._enforce_confidence(decision) for decision in decisions]
        directions = {decision.direction for decision in checked}
        if len(directions) != 1 or any(decision.fallback for decision in checked):
            detail = "；".join(
                f"{decision.provider}={decision.direction.value}"
                f"({decision.confidence:.0%})"
                for decision in checked
            )
            return OpeningDecision.flat(
                f"双模型未形成满足阈值的同向结论：{detail}",
                provider=provider_label,
                model=model_label,
                risks=("模型意见不一致或置信度不足",),
            )

        direction = checked[0].direction
        return OpeningDecision(
            direction=direction,
            confidence=min(decision.confidence for decision in checked),
            summary="；".join(
                f"{decision.provider}: {decision.summary}" for decision in checked
            ),
            factors=tuple(
                f"{decision.provider}: {factor}"
                for decision in checked
                for factor in decision.factors[:3]
            )[:6],
            risks=tuple(
                f"{decision.provider}: {risk}"
                for decision in checked
                for risk in decision.risks[:2]
            )[:5],
            provider=provider_label,
            model=model_label,
        )

    def decide_entry(
        self,
        symbol: str,
        signal: Signal,
        current_bar: Bar,
        recent_bars: tuple[Bar, ...] = (),
    ) -> EntryTimingDecision:
        provider_label = self.mode
        model_label = "+".join(client.model for client in self.clients)
        with self._context_lock:
            cached = self._daily_contexts.get(symbol.upper())
        if cached is None:
            return EntryTimingDecision.wait(
                "尚未完成当日方向决策，无法审核开仓时机",
                provider=provider_label,
                model=model_label,
                risks=("缺少当日市场上下文，已禁止本次开仓",),
            )
        day_key, day_close_time, daily_context = cached
        if not day_key <= current_bar.open_time <= day_close_time:
            return EntryTimingDecision.wait(
                "候选信号早于已缓存的交易日",
                provider=provider_label,
                model=model_label,
                risks=("交易日数据不一致，已禁止本次开仓",),
            )
        context = dict(daily_context)
        raw_strategy_context = _safe_structured_context(
            signal.strategy_context
        )
        strategy_info = raw_strategy_context.get("strategy")
        if not isinstance(strategy_info, dict):
            strategy_info = {
                "strategy_id": "UNKNOWN",
                "description": "候选信号未提供结构化策略信息",
            }
        strategy_signal_data = raw_strategy_context.get("signal")
        if not isinstance(strategy_signal_data, dict):
            strategy_signal_data = {}
        signal_age_ms = (
            max(0, int(time.time() * 1000) - current_bar.event_time)
            if current_bar.event_time > 0
            else None
        )
        context["strategy_info"] = strategy_info
        context["candidate_entry"] = {
            "symbol": signal.symbol.upper(),
            "side": signal.side.value,
            "implied_direction": (
                Direction.LONG.value
                if signal.side.value == "BUY"
                else Direction.SHORT.value
            ),
            "price": financial_text(signal.price),
            "ma_value": financial_text(signal.ma_value),
            "bar_open_time_ms": signal.bar_open_time,
            "bar_interval": current_bar.interval,
            "market_event_time_ms": (
                current_bar.event_time if current_bar.event_time > 0 else None
            ),
            "signal_age_ms": signal_age_ms,
            "strategy_reason": _clean_text(signal.reason, 700),
            "strategy_signal_data": strategy_signal_data,
        }
        context["current_intraday_bar"] = _bar_payload(current_bar)
        eligible_bars = sorted(
            {
                bar.open_time: bar
                for bar in recent_bars
                if bar.symbol.upper() == symbol.upper()
                and bar.interval == "5m"
                and bar.closed
                and bar.open_time <= current_bar.open_time
            }.values(),
            key=lambda bar: bar.open_time,
        )
        if len(eligible_bars) < self.entry_timing_bar_count:
            return EntryTimingDecision.wait(
                f"最近五分钟 K 线不足 {self.entry_timing_bar_count} 根，"
                "无法完成开仓时机审核",
                provider=provider_label,
                model=model_label,
                risks=("五分钟价格样本不足，已放弃本次开仓",),
            )
        context["today_daily_bar"] = daily_context.get("current_session")
        context["entry_timing_bar_count"] = self.entry_timing_bar_count
        context["recent_intraday_bars"] = [
            _bar_payload(bar)
            for bar in eligible_bars[-self.entry_timing_bar_count :]
        ]

        self._capture_model_input("开仓时机", context)
        if len(self.clients) == 1:
            try:
                decision = self.clients[0].decide_entry(context)
            except Exception as exc:
                return EntryTimingDecision.wait(
                    f"大模型时机决策失败：{_safe_error(exc)}",
                    provider=provider_label,
                    model=model_label,
                    risks=("模型调用失败，已放弃本次开仓",),
                )
            return self._enforce_entry_confidence(decision)

        decisions: list[EntryTimingDecision] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=len(self.clients)) as executor:
            futures = {
                executor.submit(client.decide_entry, context): client
                for client in self.clients
            }
            for future in as_completed(futures):
                client = futures[future]
                try:
                    decisions.append(future.result())
                except Exception as exc:
                    failures.append(f"{client.provider}: {_safe_error(exc)}")
        if failures or len(decisions) != len(self.clients):
            return EntryTimingDecision.wait(
                "双模型未能全部完成时机决策：" + "；".join(failures),
                provider=provider_label,
                model=model_label,
                risks=("任一模型失败时，已放弃本次开仓",),
            )

        checked = [
            self._enforce_entry_confidence(decision) for decision in decisions
        ]
        if any(not decision.enter_now for decision in checked):
            detail = "；".join(
                f"{decision.provider}="
                f"{'ENTER' if decision.enter_now else 'WAIT'}"
                f"({decision.confidence:.0%})"
                for decision in checked
            )
            return EntryTimingDecision.wait(
                f"双模型未形成入场共识：{detail}",
                provider=provider_label,
                model=model_label,
                risks=("模型意见不一致或置信度不足",),
                fallback=any(decision.fallback for decision in checked),
            )

        return EntryTimingDecision(
            enter_now=True,
            confidence=min(decision.confidence for decision in checked),
            summary="；".join(
                f"{decision.provider}: {decision.summary}"
                for decision in checked
            ),
            factors=tuple(
                f"{decision.provider}: {factor}"
                for decision in checked
                for factor in decision.factors[:3]
            )[:6],
            risks=tuple(
                f"{decision.provider}: {risk}"
                for decision in checked
                for risk in decision.risks[:2]
            )[:5],
            provider=provider_label,
            model=model_label,
        )

    def _capture_model_input(
        self, stage: str, context: dict[str, Any]
    ) -> None:
        models = "+".join(client.model for client in self.clients)
        if self.input_capture_callback is not None:
            try:
                self.input_capture_callback(
                    {
                        "今日方向": "OPENING_DIRECTION",
                        "开仓时机": "ENTRY_TIMING",
                    }.get(stage, stage),
                    self.mode,
                    models,
                    context,
                )
            except Exception:
                # Persistence/observability must never change a decision.
                pass

    def _enforce_confidence(self, decision: OpeningDecision) -> OpeningDecision:
        if decision.direction is Direction.FLAT:
            return decision
        if decision.confidence >= self.min_confidence:
            return decision
        return OpeningDecision.flat(
            f"模型置信度 {decision.confidence:.0%} 低于阈值 "
            f"{self.min_confidence:.0%}：{decision.summary}",
            provider=decision.provider,
            model=decision.model,
            risks=("置信度不足，已禁止当日新开仓",) + decision.risks[:4],
        )

    def _enforce_entry_confidence(
        self, decision: EntryTimingDecision
    ) -> EntryTimingDecision:
        if not decision.enter_now:
            return decision
        if decision.confidence >= self.min_confidence:
            return decision
        return EntryTimingDecision.wait(
            f"模型入场置信度 {decision.confidence:.0%} 低于阈值 "
            f"{self.min_confidence:.0%}：{decision.summary}",
            provider=decision.provider,
            model=decision.model,
            risks=("置信度不足，已放弃本次开仓",)
            + decision.risks[:4],
        )



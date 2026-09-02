from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Protocol

from autoquant_backend.ai_decision import (
    DecisionClient,
    DeepSeekDecisionClient,
    EntryTimingDecision,
    OpenAIResponsesDecisionClient,
    OpeningDecision,
    OpeningDecisionService,
    PublicMarketContextCollector,
    QwenDecisionClient,
)
from autoquant_shared.config import AppConfig
from autoquant_shared.formatting import financial_text
from autoquant_shared.models import (
    AiDecisionHistoryItem,
    Bar,
    Direction,
    OrderRequest,
    RunState,
    RuntimeSnapshot,
    Side,
    Signal,
)
from autoquant_backend.providers.base import TradingProvider
from autoquant_backend.providers.binance_futures import BinanceFuturesProvider
from autoquant_backend.providers.binance_stocks import (
    BinanceStocksProvider,
    OrderRejectedError,
    OrderValidationError,
)
from autoquant_backend.state import (
    OrderLedger,
    OrderRecord,
    PortfolioPerformance,
    RiskLimitError,
)
from autoquant_backend.strategies.base import Strategy
from autoquant_backend.strategies.five_minute_breakout import FiveMinuteBreakoutStrategy


SnapshotCallback = Callable[[RuntimeSnapshot], None]
LogCallback = Callable[[str, str, str], None]
FUTURES_WARMUP_BARS = 30
FIVE_MINUTE_MS = 5 * 60 * 1000


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    app: AppConfig
    api_key: str = ""
    api_secret: str = ""
    openai_api_key: str = ""
    deepseek_api_key: str = ""
    qwen_api_key: str = ""
    manual_direction: Direction = Direction.FLAT


class OpeningDecider(Protocol):
    def decide(self, symbol: str, current_daily_bar: Bar) -> OpeningDecision:
        """Return the direction filter for one exchange trading day."""


class EntryTimingDecider(Protocol):
    def decide_entry(
        self,
        symbol: str,
        signal: Signal,
        current_bar: Bar,
        recent_bars: tuple[Bar, ...] = (),
    ) -> EntryTimingDecision:
        """Return whether the current candidate signal may enter now."""


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


class SymbolRunner:
    def __init__(
        self,
        symbol: str,
        config: RunnerConfig,
        snapshot_callback: SnapshotCallback,
        log_callback: LogCallback,
        ledger: OrderLedger | None = None,
        opening_decider: OpeningDecider | None = None,
        entry_timing_decider: EntryTimingDecider | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.config = config
        self.snapshot_callback = snapshot_callback
        self.log_callback = log_callback
        self.provider = create_provider(config)
        self.strategy = create_strategy(self.symbol, config)
        self.ledger = ledger or OrderLedger()
        self._ai_trace_lock = threading.Lock()
        self._ai_trace_input = "{}"
        self._ai_trace_outputs: list[dict[str, Any]] = []
        self.opening_decider = opening_decider
        if (
            config.manual_direction is Direction.UNKNOWN
            and self.opening_decider is None
            and config.app.ai_provider != "DISABLED"
        ):
            self.opening_decider = create_opening_decider(
                config,
                model_log_callback=lambda message: self._log("AI", message),
                model_input_capture_callback=self._capture_ai_input,
                model_output_capture_callback=self._capture_ai_output,
                market_data_provider=self.provider,
            )
        self.entry_timing_decider = entry_timing_decider
        if self.entry_timing_decider is None and callable(
            getattr(self.opening_decider, "decide_entry", None)
        ):
            self.entry_timing_decider = self.opening_decider  # type: ignore[assignment]
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._snapshot = RuntimeSnapshot(symbol=self.symbol)
        self._snapshot_lock = threading.Lock()
        self._position_quantity = Decimal("0")
        self._average_entry_price = Decimal("0")
        self._position_additions = 0
        self._pending_orders = 0
        self._session_open_notional = Decimal("0")
        self._last_order_reconcile_at = 0.0
        self._order_reconcile_thread: threading.Thread | None = None
        self._order_event_thread: threading.Thread | None = None
        self._background_order_error = ""
        self._ai_decision_day_key: int | None = None
        self._daily_backfill_day_key: int | None = None
        self._warmup_backfill_day_key: int | None = None
        self._close_position_on_stop = False
        self._lifecycle_lock = threading.Lock()
        self._order_sync_lock = threading.RLock()

    @property
    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.is_alive:
                return
            self._close_position_on_stop = False
            self._session_open_notional = Decimal("0")
            with self._snapshot_lock:
                self._snapshot.session_open_notional = Decimal("0")
            self.stop_event.clear()
            self._background_order_error = ""
            self.thread = threading.Thread(
                target=self._run,
                name=f"autoquant-{self.symbol}",
                daemon=True,
            )
            self.thread.start()

    def stop(self, *, close_position: bool = False) -> None:
        with self._lifecycle_lock:
            self._close_position_on_stop = (
                self._close_position_on_stop or close_position
            )
            if self.is_alive:
                message = "正在停止并准备平仓" if close_position else "正在停止"
                self._update(RunState.STOPPING, message)
                self.stop_event.set()
                return
            if close_position and self._needs_close_handling():
                self.stop_event.set()
                self.thread = threading.Thread(
                    target=self._close_stopped_position,
                    name=f"autoquant-close-{self.symbol}",
                    daemon=True,
                )
                self.thread.start()
                return
        self._update(RunState.STOPPED, "已停止")

    def join(self, timeout: float | None = None) -> None:
        if self.thread:
            self.thread.join(timeout=timeout)

    def snapshot(self) -> RuntimeSnapshot:
        with self._snapshot_lock:
            return replace(self._snapshot)

    def _run(self) -> None:
        self._update(RunState.STARTING, "正在校验标的并连接行情")
        try:
            stale_count = self.ledger.mark_stale_submitting_unknown(self.symbol)
            if stale_count:
                self._log(
                    "ERROR",
                    f"发现 {stale_count} 笔提交中断订单，已标记为状态未知且不会自动重试",
                )
            info = self.provider.check_symbol(self.symbol)
            validation = info.get("validation")
            if validation:
                self._log("INFO", str(validation))
            else:
                self._log(
                    "INFO",
                    f"标的校验通过，tradability={info.get('tradability', 'UNKNOWN')}",
                )
            if str(info.get("contractType", "")).upper() == "TRADIFI_PERPETUAL":
                market_data_symbol = str(info.get("baseAsset", "")).strip().upper()
                set_market_data_symbol = getattr(
                    self.opening_decider, "set_market_data_symbol", None
                )
                if market_data_symbol and callable(set_market_data_symbol):
                    set_market_data_symbol(self.symbol, market_data_symbol)
                    self._log(
                        "INFO",
                        f"AI 市场数据代码映射：{self.symbol} -> "
                        f"{market_data_symbol}",
                    )
            if self.config.app.trading_mode == "REAL":
                self._reconcile_orders()
                self._start_order_event_loop()
                self._start_order_reconcile_loop()
            is_paper = self.config.app.trading_mode != "REAL"
            unknown_count = self.ledger.unknown_count(
                self.symbol, paper=is_paper
            )
            if unknown_count:
                message = (
                    f"本地账本有 {unknown_count} 笔状态未知订单。"
                    "请先在 Binance 核对并处理，再使用界面的解除锁定功能"
                )
                self._log("ERROR", message)
                if not is_paper:
                    raise RuntimeError(f"实盘安全锁定：{message}")
            self._update_risk_cache(paper=is_paper)
            self._preload_futures_warmup()
            if self.stop_event.is_set():
                return
            self._refresh_market_snapshot("等待实时 5 分钟收盘 K 线")
            for bar in self.provider.stream_bars(
                self.symbol, self.stop_event, self._on_provider_status
            ):
                if self.stop_event.is_set():
                    break
                if self._background_order_error:
                    raise RuntimeError(self._background_order_error)
                if bar.interval == "5m" and bar.closed:
                    self._update_risk_cache(paper=is_paper)
                if (
                    self.config.manual_direction is Direction.UNKNOWN
                    and bar.interval == "1d"
                ):
                    self._apply_opening_decision(bar)
                    if self.stop_event.is_set():
                        break
                signal = self.strategy.on_bar(bar)
                if (
                    self.config.manual_direction is Direction.UNKNOWN
                    and bar.interval == "1d"
                ):
                    if self.config.app.ai_provider == "DISABLED":
                        self._backfill_daily_direction(bar)
                    self._backfill_warmup(bar)
                    if self.stop_event.is_set():
                        break
                risk_signal = self._risk_exit_signal(bar)
                if risk_signal is not None:
                    signal = risk_signal
                self._refresh_market_snapshot()
                if signal is None:
                    continue
                if self._handle_signal(signal, bar, is_paper=is_paper):
                    break
            if self._background_order_error:
                raise RuntimeError(self._background_order_error)
        except Exception as exc:
            if self._background_order_error or not self.stop_event.is_set():
                message = (
                    self._background_order_error
                    or str(exc)
                    or exc.__class__.__name__
                )
                self._log("ERROR", message)
                self._update(RunState.ERROR, message)
                self.stop_event.set()
                return
        if self._close_position_on_stop and not self._force_close_position():
            return
        message = "已停止并完成平仓" if self._close_position_on_stop else "已停止"
        self._update(RunState.STOPPED, message)
        self._log("INFO", f"量化运行{message}")

    def _handle_signal(
        self,
        signal: Signal,
        bar: Bar,
        *,
        is_paper: bool,
    ) -> bool:
        """Validate and submit a signal; return whether the run loop should stop."""
        position = self.ledger.position_summary(self.symbol, paper=is_paper)
        is_exit = (
            signal.side is Side.SELL and position.quantity > 0
        ) or (
            signal.side is Side.BUY and position.quantity < 0
        )
        is_addition = position.quantity != 0 and not is_exit
        signal_message = self._signal_message(
            signal,
            is_exit=is_exit,
            is_addition=is_addition,
            position_quantity=position.quantity,
            paper=is_paper,
        )
        self._update(RunState.SIGNAL, signal_message)
        self._log("SIGNAL", signal_message)
        if self.stop_event.is_set():
            self._log("INFO", "停止请求已生效，信号不会下单")
            return True

        pending_count = self.ledger.pending_count(self.symbol, paper=is_paper)
        if pending_count:
            self._skip_order(
                f"未下单：仍有 {pending_count} 笔订单未到终态，"
                "等待成交状态确认"
            )
            return False

        supports_short = bool(getattr(self.provider, "supports_short", False))
        if (
            is_addition
            and position.additions
            >= self.config.app.max_additions_per_position
        ):
            self._skip_order(
                "未下单：本次持仓已达加仓次数上限",
                level="INFO",
            )
            return False
        if not self._signal_is_fresh(bar):
            self._skip_order("未下单：信号已超过配置的有效期")
            return False
        if not is_exit and signal.side is Side.SELL and not supports_short:
            self._skip_order("未下单：当前供应商不支持建立空头")
            return False
        if not is_paper:
            current_info = self.provider.check_symbol(self.symbol)
            if self.stop_event.is_set():
                self._log("INFO", "停止请求已生效，标的校验后不再下单")
                return True
            tradability = str(current_info.get("tradability", "NONE"))
            if tradability not in {"BUY_SELL", signal.side.value}:
                self._skip_order(
                    f"未下单：当前 tradability={tradability}，"
                    f"不允许 {signal.side.value}"
                )
                return False
        if not is_exit and self.config.app.ai_provider != "DISABLED":
            ai_allows_entry = self._ai_allows_entry(signal, bar)
            if self.stop_event.is_set():
                self._log(
                    "INFO",
                    "停止请求在大模型时机审核期间到达，不会下单",
                )
                return True
            if not ai_allows_entry:
                return False
        if not is_exit and not self._signal_is_fresh(bar):
            self._skip_order("未下单：大模型时机审核后信号已超时")
            return False

        day_key = getattr(self.strategy, "current_day_key", None)
        if day_key is None:
            self._log("ERROR", "未下单：尚未确定当前交易日")
            return False
        order = self._create_signal_order(
            signal,
            position_quantity=position.quantity,
            is_exit=is_exit,
            supports_short=supports_short,
        )
        if (
            not order.reduce_only
            and order.buy_notional
            > Decimal(self.config.app.max_order_notional)
        ):
            self._skip_order("资金风控阻止下单：开仓金额超过单笔金额上限")
            return False
        try:
            self.ledger.record_submitting(
                order,
                day_key,
                paper=is_paper,
                max_daily_buy_notional=Decimal(
                    self.config.app.max_daily_buy_notional
                ),
                max_position_additions=(
                    self.config.app.max_additions_per_position
                ),
            )
        except RiskLimitError as exc:
            message = str(exc)
            self._log("ERROR", f"资金风控阻止下单：{message}")
            self._refresh_market_snapshot(message)
            return False

        if self.stop_event.is_set():
            message = "停止请求在订单发送前到达，订单未提交"
            self.ledger.mark_rejected(order.client_order_id, message)
            self._log("INFO", message)
            return True
        self._submit_order(order, is_paper=is_paper)
        return False

    def _create_signal_order(
        self,
        signal: Signal,
        *,
        position_quantity: Decimal,
        is_exit: bool,
        supports_short: bool,
    ) -> OrderRequest:
        return OrderRequest(
            symbol=self.symbol,
            side=signal.side,
            reference_price=signal.price,
            buy_notional=(
                Decimal("0")
                if is_exit
                else Decimal(self.config.app.buy_notional)
            ),
            sell_quantity=(
                abs(position_quantity)
                if is_exit
                else Decimal("0")
            ),
            client_order_id=f"aq{uuid.uuid4().hex}",
            reduce_only=is_exit,
            allow_short=supports_short,
        )

    def _submit_order(self, order: OrderRequest, *, is_paper: bool) -> None:
        is_addition = (
            not order.reduce_only
            and self.ledger.position_summary(
                order.symbol,
                paper=is_paper,
                exclude_client_order_id=order.client_order_id,
            ).quantity
            != 0
        )
        try:
            result = self.provider.place_order(order)
        except (OrderValidationError, OrderRejectedError) as exc:
            message = str(exc) or exc.__class__.__name__
            self.ledger.mark_rejected(order.client_order_id, message)
            self._log("ERROR", f"订单未发送或已明确拒绝：{message}")
            self._refresh_market_snapshot(message)
            return
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self.ledger.mark_unknown(order.client_order_id, message)
            self._log(
                "ERROR",
                f"订单提交结果未知，已停止自动交易且不会重试：{message}",
            )
            raise

        if result.accepted:
            self.ledger.mark_acknowledged(
                order.client_order_id,
                result.order_id,
                result.message,
            )
            if not order.reduce_only:
                self._session_open_notional += order.buy_notional
            action = (
                "平仓"
                if order.reduce_only
                else "加仓"
                if is_addition
                else "开仓"
            )
            mode = "模拟" if result.paper else "实盘"
            message = f"{mode}{action}订单已提交"
            if result.paper:
                filled_quantity = (
                    order.sell_quantity
                    if order.reduce_only
                    else order.buy_notional / order.reference_price
                )
                self._record_filled_order(
                    order.client_order_id,
                    filled_quantity=filled_quantity,
                    average_price=order.reference_price,
                )
            else:
                self._log("ORDER", f"{message}，等待成交确认")
                self._reconcile_single_order(
                    order.client_order_id,
                    result.order_id,
                )
                if self.ledger.unknown_count(self.symbol, paper=False):
                    raise RuntimeError("实盘订单状态无法确认，已锁定并停止策略")
        else:
            message = f"订单被拒绝: {result.message}"
            self.ledger.mark_rejected(order.client_order_id, result.message)
            self._log("ERROR", message)
        self._update_risk_cache(paper=is_paper)
        self._refresh_market_snapshot(message)

    def _signal_message(
        self,
        signal: Signal,
        *,
        is_exit: bool,
        position_quantity: Decimal,
        paper: bool,
        is_addition: bool = False,
    ) -> str:
        action = (
            "平仓信号"
            if is_exit
            else "加仓信号"
            if is_addition
            else "开仓信号"
        )
        if is_exit:
            opening_direction = "多头" if position_quantity > 0 else "空头"
        else:
            opening_direction = "多头" if signal.side is Side.BUY else "空头"
        return (
            f"{action}｜标的 {signal.symbol.upper()}｜"
            f"交易模式 {'模拟' if paper else '实盘'}｜"
            f"开仓方向 {opening_direction}｜"
            f"价格 {financial_text(signal.price)}｜"
            f"MA {financial_text(signal.ma_value)}｜"
            f"原因 {signal.reason}"
        )

    def _skip_order(self, message: str, *, level: str = "ERROR") -> None:
        self._log(level, message)
        self._refresh_market_snapshot(message)

    def _ai_allows_entry(self, signal: Signal, bar: Bar) -> bool:
        if self.entry_timing_decider is None:
            self._skip_order(
                "未下单：大模型已启用，但开仓时机审核器未配置"
            )
            return False
        self._update(RunState.SIGNAL, "大模型正在审核当前候选开仓时机")
        self._begin_ai_trace()
        decision_started_at = time.monotonic()
        try:
            recent_bars = tuple(getattr(self.strategy, "recent_bars", ()))
            decision = self.entry_timing_decider.decide_entry(
                self.symbol,
                signal,
                bar,
                recent_bars,
            )
        except Exception as exc:
            decision = EntryTimingDecision.wait(
                f"大模型时机决策异常：{' '.join(str(exc).split())[:240]}",
                provider=self.config.app.ai_provider,
                risks=("异常已触发安全兜底，放弃本次开仓",),
            )
        elapsed_ms = max(
            0, int(round((time.monotonic() - decision_started_at) * 1000))
        )
        action = "ENTER" if decision.enter_now else "WAIT"
        self._persist_ai_decision(
            stage="ENTRY_TIMING",
            outcome=action,
            confidence=decision.confidence,
            summary=decision.summary,
            factors=decision.factors,
            risks=decision.risks,
            provider=decision.provider,
            model=decision.model,
            fallback=decision.fallback,
            elapsed_ms=elapsed_ms,
        )
        level = "ERROR" if decision.fallback else "AI"
        self._log(
            level,
            f"{decision.provider}/{decision.model or '-'} 开仓时机="
            f"{action}，置信度={decision.confidence:.0%}，"
            f"决策耗时={elapsed_ms}ms；"
            f"{decision.summary}",
        )
        if decision.factors:
            self._log("AI", "时机依据：" + "；".join(decision.factors))
        if decision.risks:
            self._log("AI", "时机风险：" + "；".join(decision.risks))
        if decision.enter_now:
            return True
        message = f"大模型决定等待后续时机：{decision.summary}"
        self._refresh_market_snapshot(message)
        return False

    def _close_stopped_position(self) -> None:
        if not self._force_close_position():
            return
        self._update(RunState.STOPPED, "已停止并完成平仓")
        self._log("INFO", "已停止的量化持仓已完成平仓")

    def _needs_close_handling(self) -> bool:
        is_paper = self.config.app.trading_mode != "REAL"
        quantity = self.ledger.position_summary(
            self.symbol, paper=is_paper
        ).quantity
        return bool(
            quantity != 0
            or self.ledger.pending_count(self.symbol, paper=is_paper)
            or self.ledger.unknown_count(self.symbol, paper=is_paper)
        )

    def _force_close_position(self) -> bool:
        is_paper = self.config.app.trading_mode != "REAL"
        mode = "模拟" if is_paper else "实盘"
        self._update(RunState.STOPPING, f"正在停止并强制平仓（{mode}）")
        try:
            if not is_paper:
                self._reconcile_orders()
            unknown_count = self.ledger.unknown_count(
                self.symbol, paper=is_paper
            )
            pending_count = self.ledger.pending_count(
                self.symbol, paper=is_paper
            )
            if unknown_count:
                raise RuntimeError(
                    f"存在 {unknown_count} 笔状态未知订单，不能安全强制平仓"
                )
            if pending_count:
                raise RuntimeError(
                    f"仍有 {pending_count} 笔订单未到终态，不能安全强制平仓"
                )

            position = self.ledger.position_summary(
                self.symbol, paper=is_paper
            )
            if position.quantity == 0:
                self._update_risk_cache(paper=is_paper)
                self._log("INFO", "停止量化时没有程序持仓，无需平仓")
                return True

            close_side = Side.SELL if position.quantity > 0 else Side.BUY
            direction = "多头" if position.quantity > 0 else "空头"

            if not is_paper:
                info = self.provider.check_symbol(self.symbol)
                tradability = str(info.get("tradability", "NONE"))
                if tradability not in {"BUY_SELL", close_side.value}:
                    raise RuntimeError(
                        f"当前 tradability={tradability}，交易所不允许 "
                        f"{close_side.value} 平仓"
                    )

            reference_price = getattr(self.strategy, "last_price", None)
            if reference_price is None or reference_price <= 0:
                reference_price = position.average_price
            if reference_price <= 0:
                raise RuntimeError("缺少有效参考价格，不能记录强制平仓订单")

            day_key = getattr(self.strategy, "current_day_key", None)
            if day_key is None:
                day_key = int(time.time() * 1000)
            order = OrderRequest(
                symbol=self.symbol,
                side=close_side,
                reference_price=reference_price,
                buy_notional=Decimal("0"),
                sell_quantity=abs(position.quantity),
                client_order_id=f"aq{uuid.uuid4().hex}",
                reduce_only=True,
                allow_short=bool(getattr(self.provider, "supports_short", False)),
            )
            self.ledger.record_submitting(
                order,
                day_key,
                paper=is_paper,
            )
            try:
                result = self.provider.place_order(order)
            except (OrderValidationError, OrderRejectedError) as exc:
                message = str(exc) or exc.__class__.__name__
                self.ledger.mark_rejected(order.client_order_id, message)
                raise RuntimeError(f"强制平仓订单被拒绝：{message}") from exc
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                self.ledger.mark_unknown(order.client_order_id, message)
                raise RuntimeError(
                    f"强制平仓提交结果未知，必须登录 Binance 核对：{message}"
                ) from exc

            if not result.accepted:
                self.ledger.mark_rejected(
                    order.client_order_id, result.message
                )
                raise RuntimeError(f"强制平仓订单被拒绝：{result.message}")

            self.ledger.mark_acknowledged(
                order.client_order_id,
                result.order_id,
                result.message,
            )
            if result.paper:
                self._record_filled_order(
                    order.client_order_id,
                    filled_quantity=order.sell_quantity,
                    average_price=order.reference_price,
                )
            else:
                self._log(
                    "ORDER",
                    f"实盘平仓订单已提交（平{direction}），等待成交确认",
                )
                self._reconcile_order_until_terminal(
                    order.client_order_id,
                    result.order_id,
                )

            self._update_risk_cache(paper=is_paper)
            if self.ledger.unknown_count(self.symbol, paper=is_paper):
                raise RuntimeError(
                    "强制平仓订单状态未知，必须登录 Binance 核对持仓"
                )
            if self._pending_orders:
                raise RuntimeError(
                    "强制平仓订单尚未到终态，必须登录 Binance 核对持仓"
                )
            if self._position_quantity != 0:
                raise RuntimeError(
                    f"强制平仓后仍有 {self._position_quantity} 股程序持仓"
                )
            self._refresh_market_snapshot("停止量化强制平仓完成")
            return True
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self._update_risk_cache(paper=is_paper)
            self._log("ERROR", f"停止量化强制平仓失败：{message}")
            self._update(RunState.ERROR, f"停止完成，但强制平仓失败：{message}")
            return False

    def _reconcile_order_until_terminal(
        self,
        client_order_id: str,
        order_id: str,
        *,
        attempts: int = 6,
        interval: float = 0.75,
    ) -> None:
        pending_statuses = {"ACKNOWLEDGED", "NEW", "ACCEPTED", "PARTIALLY_FILLED"}
        for attempt in range(max(1, attempts)):
            self._reconcile_single_order(client_order_id, order_id)
            record = self.ledger.get_record(client_order_id)
            if record is None or record.status not in pending_statuses:
                return
            if attempt + 1 < attempts:
                time.sleep(max(0.0, interval))

    def _apply_opening_decision(self, bar: Bar) -> None:
        if (
            self.opening_decider is None
            or self._ai_decision_day_key == bar.open_time
        ):
            return
        self._update(
            RunState.STARTING,
            "正在结合近期新闻、大盘和个股走势生成今日方向",
        )
        self._begin_ai_trace()
        decision_started_at = time.monotonic()
        try:
            decision = self.opening_decider.decide(self.symbol, bar)
        except Exception as exc:
            decision = OpeningDecision.flat(
                f"大模型决策异常：{' '.join(str(exc).split())[:240]}",
                provider=self.config.app.ai_provider,
                risks=("异常已触发安全兜底，今日不开新仓",),
            )
        elapsed_ms = max(
            0, int(round((time.monotonic() - decision_started_at) * 1000))
        )
        self._persist_ai_decision(
            stage="OPENING_DIRECTION",
            outcome=decision.direction.value,
            confidence=decision.confidence,
            summary=decision.summary,
            factors=decision.factors,
            risks=decision.risks,
            provider=decision.provider,
            model=decision.model,
            fallback=decision.fallback,
            elapsed_ms=elapsed_ms,
        )
        self._ai_decision_day_key = bar.open_time
        setter = getattr(self.strategy, "set_opening_direction", None)
        if not callable(setter):
            raise RuntimeError("当前策略不支持大模型开仓方向过滤")
        setter(decision.direction, decision.summary)
        level = "ERROR" if decision.fallback else "AI"
        self._log(
            level,
            f"{decision.provider}/{decision.model or '-'} 今日方向="
            f"{decision.direction.value}，置信度={decision.confidence:.0%}，"
            f"决策耗时={elapsed_ms}ms；"
            f"{decision.summary}",
        )
        if decision.factors:
            self._log("AI", "主要依据：" + "；".join(decision.factors))
        if decision.risks:
            self._log("AI", "主要风险：" + "；".join(decision.risks))

    def _begin_ai_trace(self) -> None:
        with self._ai_trace_lock:
            self._ai_trace_input = "{}"
            self._ai_trace_outputs = []

    def _capture_ai_input(
        self,
        stage: str,
        provider: str,
        models: str,
        context: dict[str, Any],
    ) -> None:
        serialized = json.dumps(
            {
                "stage": stage,
                "provider": provider,
                "models": models,
                "context": context,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._ai_trace_lock:
            self._ai_trace_input = serialized

    def _capture_ai_output(
        self,
        stage: str,
        provider: str,
        model: str,
        response: dict[str, Any],
        response_ms: int = 0,
    ) -> None:
        envelope = {
            "stage": stage,
            "provider": provider,
            "model": model,
            "response_ms": max(0, int(response_ms)),
            "response": json.loads(
                json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            ),
        }
        with self._ai_trace_lock:
            self._ai_trace_outputs.append(envelope)

    def _consume_ai_trace(self) -> tuple[str, str, int]:
        with self._ai_trace_lock:
            input_json = self._ai_trace_input
            response_by_provider: dict[str, int] = {}
            for item in self._ai_trace_outputs:
                provider = str(item.get("provider", ""))
                response_by_provider[provider] = response_by_provider.get(
                    provider, 0
                ) + max(0, int(item.get("response_ms", 0)))
            response_ms = max(response_by_provider.values(), default=0)
            output_json = json.dumps(
                self._ai_trace_outputs,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return input_json, output_json, response_ms

    def _persist_ai_decision(
        self,
        *,
        stage: str,
        outcome: str,
        confidence: float,
        summary: str,
        factors: tuple[str, ...],
        risks: tuple[str, ...],
        provider: str,
        model: str,
        fallback: bool,
        elapsed_ms: int,
    ) -> None:
        input_json, output_json, response_ms = self._consume_ai_trace()
        try:
            self.ledger.record_ai_decision(
                AiDecisionHistoryItem(
                    record_id=str(uuid.uuid4()),
                    decided_at=int(time.time() * 1000),
                    symbol=self.symbol,
                    stage=stage,
                    provider=provider,
                    model=model,
                    outcome=outcome,
                    confidence=float(confidence),
                    summary=summary,
                    factors=factors,
                    risks=risks,
                    input_json=input_json,
                    output_json=output_json,
                    fallback=fallback,
                    elapsed_ms=elapsed_ms,
                    response_ms=response_ms,
                )
            )
        except Exception as exc:
            self._log(
                "ERROR",
                "AI 决策记录持久化失败："
                + " ".join(str(exc).split())[:240],
            )

    def _backfill_warmup(self, daily_bar: Bar) -> None:
        if self._warmup_backfill_day_key == daily_bar.open_time:
            return
        self._warmup_backfill_day_key = daily_bar.open_time
        required = int(getattr(self.strategy, "warmup_required", 0))
        current = int(getattr(self.strategy, "warmup_bars", 0))
        ai_enabled = self.config.app.ai_provider != "DISABLED"
        context_required = (
            self.config.app.ai_entry_timing_bars if ai_enabled else required
        )
        recent_count = len(tuple(getattr(self.strategy, "recent_bars", ())))
        if required <= 0 or (
            current >= required and recent_count >= context_required
        ):
            return
        fetcher = getattr(self.provider, "get_historical_bars", None)
        if not callable(fetcher):
            return
        end_time = min(daily_bar.close_time, int(time.time() * 1000) - 1)
        if end_time < daily_bar.open_time:
            return

        start_time = daily_bar.open_time
        if ai_enabled:
            # Include earlier sessions so an early-session candidate can still
            # carry a complete 60-bar timing context.
            start_time -= 14 * 86_400_000
        self._update(
            RunState.WARMING_UP,
            f"正在加载最近 {context_required} 根历史 5 分钟 K 线",
        )
        try:
            bars = fetcher(
                self.symbol,
                "5m",
                start_time,
                end_time,
                context_required,
            )
        except Exception as exc:
            message = " ".join(str(exc).split())[:300]
            self._log(
                "ERROR",
                f"历史 K 线回补失败，将继续等待实时行情：{message}",
            )
            return

        seed_recent = getattr(self.strategy, "seed_recent_bars", None)
        if ai_enabled and callable(seed_recent):
            seed_recent(bars)
        for historical_bar in bars:
            if self.stop_event.is_set():
                return
            # Historical signals are intentionally discarded: the bars only seed
            # indicator state and must never cause a retroactive order.
            self.strategy.on_bar(historical_bar)
        loaded = int(getattr(self.strategy, "warmup_bars", 0))
        recent_count = len(tuple(getattr(self.strategy, "recent_bars", ())))
        self._log(
            "INFO",
            f"历史 K 线回补完成，指标预热 {loaded}/{required}，"
            f"大模型时机样本 {recent_count}/{context_required}",
        )

    def _preload_futures_warmup(self) -> None:
        if self.config.app.provider != "binance_futures":
            return
        # AI direction mode needs its current daily candle before the strategy
        # accepts intraday bars, so it uses the daily callback backfill below.
        if self.config.manual_direction is Direction.UNKNOWN:
            return
        fetcher = getattr(self.provider, "get_historical_bars", None)
        if not callable(fetcher):
            return

        now_ms = int(time.time() * 1000)
        current_bar_open = now_ms - (now_ms % FIVE_MINUTE_MS)
        end_time = current_bar_open - 1
        start_time = current_bar_open - (
            FUTURES_WARMUP_BARS * FIVE_MINUTE_MS
        )
        self._update(
            RunState.WARMING_UP,
            f"正在加载实时前 {FUTURES_WARMUP_BARS} 根 Futures 5 分钟 K 线",
        )
        try:
            bars = fetcher(
                self.symbol,
                "5m",
                start_time,
                end_time,
                FUTURES_WARMUP_BARS,
            )
        except Exception as exc:
            message = " ".join(str(exc).split())[:300]
            self._log(
                "ERROR",
                f"Futures 历史 K 线预热失败，将继续等待实时行情：{message}",
            )
            return

        for historical_bar in bars:
            if self.stop_event.is_set():
                return
            # Seed indicators only. A signal found before the real-time stream
            # must never submit a retroactive order.
            self.strategy.on_bar(historical_bar)
        loaded = int(getattr(self.strategy, "warmup_bars", 0))
        required = int(getattr(self.strategy, "warmup_required", 0))
        self._log(
            "INFO",
            f"Futures 历史 K 线预热完成，获取 {len(bars)} 根，"
            f"指标进度 {loaded}/{required}",
        )

    def _backfill_daily_direction(self, current_daily_bar: Bar) -> None:
        if self._daily_backfill_day_key == current_daily_bar.open_time:
            return
        self._daily_backfill_day_key = current_daily_bar.open_time
        setter = getattr(self.strategy, "seed_daily_history", None)
        fetcher = getattr(self.provider, "get_historical_bars", None)
        if not callable(setter):
            return
        if not callable(fetcher):
            self._use_manual_direction_fallback("供应商不支持历史日线查询")
            return

        self._update(RunState.WARMING_UP, "正在加载前两个交易日的日线")
        try:
            bars = fetcher(
                self.symbol,
                "1d",
                current_daily_bar.open_time - 14 * 86_400_000,
                current_daily_bar.open_time - 1,
                2,
            )
            setter(bars)
        except Exception as exc:
            message = " ".join(str(exc).split())[:300]
            self._use_manual_direction_fallback(message)
            return

        direction = getattr(self.strategy, "direction", None)
        direction_value = getattr(direction, "value", "UNKNOWN")
        if direction is Direction.UNKNOWN:
            self._use_manual_direction_fallback(
                f"返回的 {len(bars)} 根日线未形成两个有效的前序交易日"
            )
            direction = getattr(self.strategy, "direction", None)
            direction_value = getattr(direction, "value", "UNKNOWN")
        self._log(
            "INFO",
            f"历史日线回补完成，加载 {len(bars)} 根，今日方向 {direction_value}",
        )

    def _use_manual_direction_fallback(self, failure_message: str) -> None:
        current_direction = getattr(self.strategy, "direction", Direction.UNKNOWN)
        if current_direction is not Direction.UNKNOWN:
            if getattr(self.strategy, "direction_source", "") == "MANUAL":
                self._log(
                    "ERROR",
                    f"历史日线不可用，继续使用手动开仓方向 "
                    f"{current_direction.value}：{failure_message}",
                )
                return
            self._log(
                "ERROR",
                f"历史日线回补失败，但已有方向 {current_direction.value}，"
                f"不启用手动回退：{failure_message}",
            )
            return
        manual_direction = self.config.manual_direction
        if manual_direction is Direction.UNKNOWN:
            self._log(
                "ERROR",
                f"历史日线回补失败且未设置手动方向，今日方向保持 UNKNOWN："
                f"{failure_message}",
            )
            return
        setter = getattr(self.strategy, "set_fallback_direction", None)
        if not callable(setter):
            self._log("ERROR", "当前策略不支持手动开仓方向回退")
            return
        setter(manual_direction, failure_message)
        self._log(
            "ERROR",
            f"历史日线不可用，已采用手动开仓方向 {manual_direction.value}："
            f"{failure_message}",
        )

    def _update_risk_cache(self, *, paper: bool) -> None:
        previous_quantity = self._position_quantity
        position = self.ledger.position_summary(self.symbol, paper=paper)
        self._position_quantity = position.quantity
        self._average_entry_price = position.average_price
        self._position_additions = position.additions
        if previous_quantity != 0 and position.quantity == 0:
            self._session_open_notional = Decimal("0")
        self._pending_orders = self.ledger.pending_count(
            self.symbol, paper=paper
        )

    def _reconcile_orders(self) -> None:
        for record in self.ledger.unresolved_with_order_id(self.symbol):
            self._reconcile_record(record)
        self._last_order_reconcile_at = time.monotonic()

    def _start_order_reconcile_loop(self) -> None:
        thread = self._order_reconcile_thread
        if thread is not None and thread.is_alive():
            return
        self._order_reconcile_thread = threading.Thread(
            target=self._order_reconcile_loop,
            name=f"autoquant-orders-{self.symbol}",
            daemon=True,
        )
        self._order_reconcile_thread.start()

    def _start_order_event_loop(self) -> None:
        streamer = getattr(self.provider, "stream_order_updates", None)
        if not callable(streamer):
            return
        thread = self._order_event_thread
        if thread is not None and thread.is_alive():
            return
        self._order_event_thread = threading.Thread(
            target=self._order_event_loop,
            name=f"autoquant-order-events-{self.symbol}",
            daemon=True,
        )
        self._order_event_thread.start()

    def _order_event_loop(self) -> None:
        streamer = getattr(self.provider, "stream_order_updates", None)
        if not callable(streamer):
            return
        try:
            for event in streamer(self.stop_event, self._on_provider_status):
                if self.stop_event.is_set():
                    return
                if str(event.get("e", "")).upper() != "ORDER_TRADE_UPDATE":
                    continue
                order = event.get("o")
                if not isinstance(order, dict):
                    continue
                if str(order.get("s", "")).upper() != self.symbol:
                    continue
                client_order_id = str(order.get("c", "")).strip()
                record = self.ledger.get_record(client_order_id)
                if (
                    record is None
                    or not record.order_id
                    or record.status
                    in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
                ):
                    continue
                self._reconcile_record(record)
                self._update_risk_cache(paper=False)
        except Exception as exc:
            if not self.stop_event.is_set():
                self._log(
                    "ERROR",
                    "订单事件流不可用，继续使用 REST 轮询："
                    f"{str(exc) or exc.__class__.__name__}",
                )

    def _order_reconcile_loop(self) -> None:
        fallback_interval = (
            30.0
            if callable(getattr(self.provider, "stream_order_updates", None))
            else 5.0
        )
        while not self.stop_event.wait(fallback_interval):
            try:
                if self.ledger.pending_count(self.symbol, paper=False) <= 0:
                    continue
                self._reconcile_orders()
                self._update_risk_cache(paper=False)
                became_unknown = bool(
                    self.ledger.unknown_count(self.symbol, paper=False)
                )
            except Exception as exc:
                self._background_order_error = (
                    "实盘订单状态核对失败，已锁定并停止策略："
                    f"{str(exc) or exc.__class__.__name__}"
                )
                self.stop_event.set()
                return
            if became_unknown:
                self._background_order_error = (
                    "实盘订单状态变为未知，已锁定并停止策略"
                )
                self.stop_event.set()
                return

    def _reconcile_single_order(
        self,
        client_order_id: str,
        order_id: str,
    ) -> None:
        record = self.ledger.get_record(client_order_id)
        if record is None:
            self._log("ERROR", "本地账本找不到待同步订单")
            return
        self._reconcile_record(record)
        self._last_order_reconcile_at = time.monotonic()

    def _reconcile_record(self, record: OrderRecord) -> None:
        with self._order_sync_lock:
            current = self.ledger.get_record(record.client_order_id)
            if current is None or current.status not in {
                "ACKNOWLEDGED",
                "NEW",
                "ACCEPTED",
                "PARTIALLY_FILLED",
            }:
                return
            self._reconcile_record_unlocked(current)

    def _reconcile_record_unlocked(self, record: OrderRecord) -> None:
        try:
            payload = self.provider.get_order_detail(
                record.order_id, record.symbol
            )
            detail = payload.get("data", payload)
            if not isinstance(detail, dict):
                raise ValueError("订单详情返回结构不符合预期")
            status = str(
                detail.get("status", detail.get("orderStatus", ""))
            ).strip().upper()
            allowed_statuses = {
                "NEW",
                "ACCEPTED",
                "PARTIALLY_FILLED",
                "FILLED",
                "CANCELED",
                "EXPIRED",
                "REJECTED",
            }
            if status not in allowed_statuses:
                raise ValueError(f"未知订单状态 {status or '<empty>'}")
            filled_quantity = self._detail_decimal(
                detail,
                "executedQty",
                "executedQuantity",
                "filledQty",
                "filledQuantity",
            )
            average_price = self._detail_decimal(
                detail,
                "avgFilledPrice",
                "avgPrice",
                "averagePrice",
                "price",
            )
            fee = self._detail_decimal(detail, "fee", "commission")
            quote_quantity = self._detail_decimal(
                detail,
                "cummulativeQuoteQty",
                "cumulativeQuoteQty",
                "executedNotional",
                "filledNotional",
            )
            if average_price <= 0 and filled_quantity > 0 and quote_quantity > 0:
                average_price = quote_quantity / filled_quantity
            if status == "FILLED" and filled_quantity <= 0:
                raise ValueError("已成交订单缺少成交数量，保持安全锁定")
            if filled_quantity > 0 and average_price <= 0:
                raise ValueError("成交订单缺少成交均价，保持安全锁定")
            if status == "FILLED":
                self._record_filled_order(
                    record.client_order_id,
                    filled_quantity=filled_quantity,
                    average_price=average_price,
                    fee=fee,
                )
            else:
                self.ledger.mark_lifecycle(
                    record.client_order_id,
                    status,
                    f"从 Binance 同步为 {status}",
                    filled_quantity=filled_quantity,
                    average_price=average_price,
                    fee=fee,
                )
                action = "平仓" if record.reduce_only else "开仓"
                self._log("INFO", f"{action}订单状态已同步为 {status}")
        except ValueError as exc:
            self.ledger.mark_unknown(record.client_order_id, str(exc))
            self._log(
                "ERROR",
                f"订单无法安全解析，已转为未知状态并锁定：{exc}",
            )
        except Exception as exc:
            self._log(
                "ERROR",
                f"无法同步订单状态，继续锁定后续下单：{exc}",
            )

    def _record_filled_order(
        self,
        client_order_id: str,
        *,
        filled_quantity: Decimal,
        average_price: Decimal,
        fee: Decimal = Decimal("0"),
    ) -> None:
        record = self.ledger.get_record(client_order_id)
        if record is None:
            raise ValueError("本地账本找不到已成交订单")
        position = self.ledger.position_summary(
            record.symbol,
            paper=record.paper,
            exclude_client_order_id=record.client_order_id,
        )
        profit = Decimal("0")
        opening_direction = (
            "多头" if record.side == Side.BUY.value else "空头"
        )
        if record.reduce_only and position.quantity != 0:
            opening_direction = "多头" if position.quantity > 0 else "空头"
            before = abs(position.quantity)
            closed = min(filled_quantity, before)
            allocated_open_fee = (
                position.open_fee * closed / before
                if before
                else Decimal("0")
            )
            allocated_close_fee = (
                fee * closed / filled_quantity
                if filled_quantity
                else Decimal("0")
            )
            if position.quantity > 0:
                profit = (
                    (average_price - position.average_price) * closed
                    - allocated_open_fee
                    - allocated_close_fee
                )
            else:
                profit = (
                    (position.average_price - average_price) * closed
                    - allocated_open_fee
                    - allocated_close_fee
                )
        self.ledger.mark_lifecycle(
            client_order_id,
            "FILLED",
            "订单已成交",
            filled_quantity=filled_quantity,
            average_price=average_price,
            fee=fee,
            realized_pnl=profit,
        )
        action = (
            "平仓"
            if record.reduce_only
            else "加仓"
            if position.quantity != 0
            else "开仓"
        )
        amount = filled_quantity * average_price
        self._log(
            "ORDER",
            f"{action}成交｜标的 {record.symbol}｜"
            f"交易模式 {'模拟' if record.paper else '实盘'}｜"
            f"开仓方向 {opening_direction}｜"
            f"价格 {financial_text(average_price)}｜"
            f"数量 {self._quantity_text(filled_quantity)}｜"
            f"金额 {financial_text(amount)}｜"
            f"收益 {financial_text(profit)}",
        )

    @staticmethod
    def _quantity_text(value: Decimal) -> str:
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _detail_decimal(detail: dict, *names: str) -> Decimal:
        for name in names:
            value = detail.get(name)
            if value in (None, ""):
                continue
            try:
                parsed = Decimal(str(value))
                if parsed.is_finite() and parsed >= 0:
                    return parsed
            except ArithmeticError:
                continue
        return Decimal("0")

    def _risk_exit_signal(self, bar: Bar) -> Signal | None:
        if bar.interval != "5m":
            return None
        quantity = self._position_quantity
        average_price = self._average_entry_price
        if quantity == 0 or average_price <= 0:
            return None
        stop_percent = Decimal(self.config.app.stop_loss_percent) / Decimal("100")
        take_percent = Decimal(self.config.app.take_profit_percent) / Decimal("100")
        if quantity > 0:
            stop_price = average_price * (Decimal("1") - stop_percent)
            take_price = average_price * (Decimal("1") + take_percent)
            side = Side.SELL
            direction = "多头"
            if bar.close <= stop_price:
                trigger = (
                    f"当前价 {financial_text(bar.close)} <= "
                    f"止损价 {financial_text(stop_price)}"
                )
                label = "止损"
            elif bar.close >= take_price:
                trigger = (
                    f"当前价 {financial_text(bar.close)} >= "
                    f"止盈价 {financial_text(take_price)}"
                )
                label = "止盈"
            else:
                return None
        else:
            stop_price = average_price * (Decimal("1") + stop_percent)
            take_price = average_price * (Decimal("1") - take_percent)
            side = Side.BUY
            direction = "空头"
            if bar.close >= stop_price:
                trigger = (
                    f"当前价 {financial_text(bar.close)} >= "
                    f"止损价 {financial_text(stop_price)}"
                )
                label = "止损"
            elif bar.close <= take_price:
                trigger = (
                    f"当前价 {financial_text(bar.close)} <= "
                    f"止盈价 {financial_text(take_price)}"
                )
                label = "止盈"
            else:
                return None
        reason = (
            f"风险{label}：{trigger}，平掉 {abs(quantity)} 股{direction}"
        )
        return Signal(
            symbol=self.symbol,
            side=side,
            price=bar.close,
            ma_value=getattr(self.strategy, "ma_value", None) or bar.close,
            bar_open_time=bar.open_time,
            reason=reason,
        )

    def _signal_is_fresh(self, bar: Bar) -> bool:
        if bar.event_time <= 0:
            return True
        age_ms = int(time.time() * 1000) - bar.event_time
        return age_ms <= self.config.app.max_signal_age_seconds * 1000

    def _refresh_market_snapshot(self, message: str | None = None) -> None:
        warmup_bars = getattr(self.strategy, "warmup_bars", 0)
        warmup_required = getattr(self.strategy, "warmup_required", 0)
        direction = getattr(self.strategy, "direction", self._snapshot.direction)
        last_price = getattr(self.strategy, "last_price", None)
        ma_value = getattr(self.strategy, "ma_value", None)
        market_prices = (
            {self.symbol: last_price}
            if last_price is not None and last_price.is_finite() and last_price > 0
            else {}
        )
        performance = self.ledger.portfolio_performance(
            paper=self.config.app.trading_mode != "REAL",
            market_prices=market_prices,
            symbol=self.symbol,
        )
        profit = (
            None
            if performance.unrealized_pnl is None
            else performance.realized_pnl + performance.unrealized_pnl
        )
        current_day_key = getattr(self.strategy, "current_day_key", None)
        ready = (
            current_day_key is not None
            and warmup_bars >= warmup_required
            and direction is not Direction.UNKNOWN
        )
        state = RunState.RUNNING if ready else RunState.WARMING_UP
        if message is None:
            if ready:
                message = f"运行中；实际方向 {direction.value}"
            elif current_day_key is None:
                if direction is Direction.UNKNOWN:
                    message = "等待当日日线和开仓方向"
                else:
                    message = (
                        "等待首根实时 5 分钟收盘 K 线；"
                        f"手动方向 {direction.value} 已设置"
                    )
            elif direction is Direction.UNKNOWN:
                message = (
                    f"实时 K 线 {warmup_bars}/{warmup_required}，等待开仓方向"
                )
            else:
                message = (
                    f"实时 K 线 {warmup_bars}/{warmup_required}；手动方向 "
                    f"{direction.value}"
                )
        with self._snapshot_lock:
            self._snapshot.state = state
            self._snapshot.direction = direction
            self._snapshot.last_price = last_price
            self._snapshot.ma_value = ma_value
            self._snapshot.warmup_bars = warmup_bars
            self._snapshot.warmup_required = warmup_required
            self._snapshot.position_additions = self._position_additions
            self._snapshot.position_quantity = self._position_quantity
            self._snapshot.average_entry_price = self._average_entry_price
            self._snapshot.pending_orders = self._pending_orders
            self._snapshot.session_open_notional = self._session_open_notional
            self._snapshot.realized_pnl = performance.realized_pnl
            self._snapshot.unrealized_pnl = performance.unrealized_pnl
            self._snapshot.profit = profit
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
        if state in {RunState.STOPPED, RunState.ERROR}:
            self._session_open_notional = Decimal("0")
        with self._snapshot_lock:
            self._snapshot.state = state
            self._snapshot.session_open_notional = self._session_open_notional
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

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Callable, Protocol

from autoquant_backend.ai_decision import (
    DecisionClient,
    DeepSeekDecisionClient,
    OpenAIResponsesDecisionClient,
    OpeningDecision,
    OpeningDecisionService,
    PublicMarketContextCollector,
)
from autoquant_shared.config import AppConfig
from autoquant_shared.models import (
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
FUTURES_WARMUP_BARS = 6
FIVE_MINUTE_MS = 5 * 60 * 1000


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    app: AppConfig
    api_key: str = ""
    api_secret: str = ""
    openai_api_key: str = ""
    deepseek_api_key: str = ""
    manual_direction: Direction = Direction.FLAT


class OpeningDecider(Protocol):
    def decide(self, symbol: str, current_daily_bar: Bar) -> OpeningDecision:
        """Return the direction filter for one exchange trading day."""


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
            ma_period=config.app.ma_period,
            max_trades_per_day=config.app.max_trades_per_day,
            manual_direction=(
                None
                if config.manual_direction is Direction.UNKNOWN
                else config.manual_direction
            ),
        )
    raise ValueError(f"未知策略: {config.app.strategy}")


def create_opening_decider(config: RunnerConfig) -> OpeningDecider | None:
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
            )
        )
    collector = PublicMarketContextCollector(
        history_days=config.app.ai_history_days,
        news_days=config.app.ai_news_days,
        news_limit=config.app.ai_news_limit,
        timeout_seconds=config.app.ai_timeout_seconds,
    )
    return OpeningDecisionService(
        collector=collector,
        clients=tuple(clients),
        min_confidence=float(Decimal(config.app.ai_min_confidence)),
        mode=mode,
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
    ) -> None:
        self.symbol = symbol.upper()
        self.config = config
        self.provider = create_provider(config)
        self.strategy = create_strategy(self.symbol, config)
        self.opening_decider = opening_decider
        if (
            config.manual_direction is Direction.UNKNOWN
            and self.opening_decider is None
            and config.app.ai_provider != "DISABLED"
        ):
            self.opening_decider = create_opening_decider(config)
        self.snapshot_callback = snapshot_callback
        self.log_callback = log_callback
        self.ledger = ledger or OrderLedger()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._snapshot = RuntimeSnapshot(symbol=self.symbol)
        self._snapshot_lock = threading.Lock()
        self._position_quantity = Decimal("0")
        self._average_entry_price = Decimal("0")
        self._pending_orders = 0
        self._daily_buy_notional = Decimal("0")
        self._last_order_reconcile_at = 0.0
        self._ai_decision_day_key: int | None = None
        self._daily_backfill_day_key: int | None = None
        self._warmup_backfill_day_key: int | None = None
        self._close_position_on_stop = False
        self._lifecycle_lock = threading.Lock()

    @property
    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.is_alive:
                return
            self._close_position_on_stop = False
            self.stop_event.clear()
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
            if self.config.app.trading_mode == "REAL":
                self._reconcile_orders()
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
                if (
                    not is_paper
                    and time.monotonic() - self._last_order_reconcile_at >= 5
                    and self._pending_orders > 0
                ):
                    self._reconcile_orders()
                    self._update_risk_cache(paper=False)
                    self._last_order_reconcile_at = time.monotonic()
                    if self.ledger.unknown_count(self.symbol, paper=False):
                        raise RuntimeError(
                            "实盘订单状态变为未知，已锁定并停止策略"
                        )
                if bar.interval == "5m" and bar.closed:
                    self._sync_trade_count()
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
                    self._backfill_daily_direction(bar)
                    self._backfill_warmup(bar)
                    if self.stop_event.is_set():
                        break
                risk_signal = self._risk_exit_signal(bar)
                if risk_signal is not None:
                    signal = risk_signal
                if bar.interval == "1d":
                    self._sync_trade_count()
                self._refresh_market_snapshot()
                if signal is None:
                    continue

                self._update(RunState.SIGNAL, signal.reason)
                self._log("SIGNAL", signal.reason)
                if self.stop_event.is_set():
                    self._log("INFO", "停止请求已生效，信号不会下单")
                    break

                position = self.ledger.position_summary(
                    self.symbol, paper=is_paper
                )
                pending_count = self.ledger.pending_count(
                    self.symbol, paper=is_paper
                )
                if pending_count:
                    message = (
                        f"未下单：仍有 {pending_count} 笔订单未到终态，"
                        "等待成交状态确认"
                    )
                    self._log("ERROR", message)
                    self._refresh_market_snapshot(message)
                    continue
                is_long_exit = signal.side is Side.SELL and position.quantity > 0
                is_short_exit = signal.side is Side.BUY and position.quantity < 0
                is_exit = is_long_exit or is_short_exit
                supports_short = bool(
                    getattr(self.provider, "supports_short", False)
                )
                if position.quantity != 0 and not is_exit:
                    direction = "多头" if position.quantity > 0 else "空头"
                    message = (
                        f"未下单：当前已有程序管理的{direction}持仓，"
                        "禁止双向或重复开仓"
                    )
                    self._log("ERROR", message)
                    self._refresh_market_snapshot(message)
                    continue
                if (
                    not is_exit
                    and self.strategy.trades_today
                    >= self.config.app.max_trades_per_day
                ):
                    message = "未下单：已达到当日入场次数上限"
                    self._log("INFO", message)
                    self._refresh_market_snapshot(message)
                    continue
                if not self._signal_is_fresh(bar):
                    message = "未下单：信号已超过配置的有效期"
                    self._log("ERROR", message)
                    self._refresh_market_snapshot(message)
                    continue
                if (
                    not is_exit
                    and signal.side is Side.SELL
                    and not supports_short
                ):
                    message = "未下单：当前供应商不支持建立空头"
                    self._log("ERROR", message)
                    self._refresh_market_snapshot(message)
                    continue
                if self.config.app.trading_mode == "REAL":
                    current_info = self.provider.check_symbol(self.symbol)
                    if self.stop_event.is_set():
                        self._log("INFO", "停止请求已生效，标的校验后不再下单")
                        break
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
                day_key = getattr(self.strategy, "current_day_key", None)
                if day_key is None:
                    self._log("ERROR", "未下单：尚未确定当前交易日")
                    continue
                order = OrderRequest(
                    symbol=self.symbol,
                    side=signal.side,
                    reference_price=signal.price,
                    buy_notional=(
                        Decimal("0")
                        if is_exit
                        else Decimal(self.config.app.buy_notional)
                    ),
                    sell_quantity=(
                        abs(position.quantity)
                        if is_exit
                        else Decimal(self.config.app.sell_quantity)
                    ),
                    client_order_id=f"aq{uuid.uuid4().hex}",
                    reduce_only=is_exit,
                    allow_short=supports_short,
                )
                if (
                    not order.reduce_only
                    and order.buy_notional
                    > Decimal(self.config.app.max_order_notional)
                ):
                    message = "资金风控阻止下单：开仓金额超过单笔金额上限"
                    self._log("ERROR", message)
                    self._refresh_market_snapshot(message)
                    continue
                try:
                    self.ledger.record_submitting(
                        order,
                        day_key,
                        paper=is_paper,
                        max_daily_buy_notional=Decimal(
                            self.config.app.max_daily_buy_notional
                        ),
                    )
                except RiskLimitError as exc:
                    message = str(exc)
                    self._log("ERROR", f"资金风控阻止下单：{message}")
                    self._refresh_market_snapshot(message)
                    continue
                self._sync_trade_count()
                if self.stop_event.is_set():
                    message = "停止请求在订单发送前到达，订单未提交"
                    self.ledger.mark_rejected(order.client_order_id, message)
                    self._sync_trade_count()
                    self._log("INFO", message)
                    break
                try:
                    result = self.provider.place_order(order)
                except (OrderValidationError, OrderRejectedError) as exc:
                    message = str(exc) or exc.__class__.__name__
                    self.ledger.mark_rejected(order.client_order_id, message)
                    self._sync_trade_count()
                    self._log("ERROR", f"订单未发送或已明确拒绝：{message}")
                    self._refresh_market_snapshot(message)
                    continue
                except Exception as exc:
                    message = str(exc) or exc.__class__.__name__
                    self.ledger.mark_unknown(order.client_order_id, message)
                    self._sync_trade_count()
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
                    mode = "模拟" if result.paper else "实盘"
                    message = f"{mode}订单已接受: {result.order_id}；{result.message}"
                    self._log("ORDER", message)
                    if result.paper:
                        filled_quantity = (
                            order.sell_quantity
                            if order.reduce_only
                            else order.buy_notional / order.reference_price
                        )
                        self.ledger.mark_lifecycle(
                            order.client_order_id,
                            "FILLED",
                            "模拟订单已成交",
                            filled_quantity=filled_quantity,
                            average_price=order.reference_price,
                        )
                    else:
                        self._reconcile_single_order(
                            order.client_order_id,
                            result.order_id,
                        )
                        if self.ledger.unknown_count(
                            self.symbol, paper=False
                        ):
                            raise RuntimeError(
                                "实盘订单状态无法确认，已锁定并停止策略"
                            )
                else:
                    message = f"订单被拒绝: {result.message}"
                    self.ledger.mark_rejected(order.client_order_id, result.message)
                    self._log("ERROR", message)
                self._sync_trade_count()
                self._update_risk_cache(paper=is_paper)
                self._refresh_market_snapshot(message)
        except Exception as exc:
            if not self.stop_event.is_set():
                message = str(exc) or exc.__class__.__name__
                self._log("ERROR", message)
                self._update(RunState.ERROR, message)
                return
        if self._close_position_on_stop and not self._force_close_position():
            return
        message = "已停止并完成平仓" if self._close_position_on_stop else "已停止"
        self._update(RunState.STOPPED, message)
        self._log("INFO", f"量化运行{message}")

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
            self._log(
                "ORDER",
                f"停止量化强制平仓订单已接受: {result.order_id}；"
                f"{order.side.value} {order.sell_quantity}（平{direction}）",
            )
            if result.paper:
                self.ledger.mark_lifecycle(
                    order.client_order_id,
                    "FILLED",
                    "模拟强制平仓订单已成交",
                    filled_quantity=order.sell_quantity,
                    average_price=order.reference_price,
                )
            else:
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
        try:
            decision = self.opening_decider.decide(self.symbol, bar)
        except Exception as exc:
            decision = OpeningDecision.flat(
                f"大模型决策异常：{' '.join(str(exc).split())[:240]}",
                provider=self.config.app.ai_provider,
                risks=("异常已触发安全兜底，今日不开新仓",),
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
            f"{decision.direction.value}，置信度={decision.confidence:.0%}；"
            f"{decision.summary}",
        )
        if decision.factors:
            self._log("AI", "主要依据：" + "；".join(decision.factors))
        if decision.risks:
            self._log("AI", "主要风险：" + "；".join(decision.risks))

    def _backfill_warmup(self, daily_bar: Bar) -> None:
        if self._warmup_backfill_day_key == daily_bar.open_time:
            return
        self._warmup_backfill_day_key = daily_bar.open_time
        required = int(getattr(self.strategy, "warmup_required", 0))
        current = int(getattr(self.strategy, "warmup_bars", 0))
        if required <= 0 or current >= required:
            return
        fetcher = getattr(self.provider, "get_historical_bars", None)
        if not callable(fetcher):
            return
        end_time = min(daily_bar.close_time, int(time.time() * 1000) - 1)
        if end_time < daily_bar.open_time:
            return

        self._update(RunState.WARMING_UP, "正在加载当日历史 5 分钟 K 线")
        try:
            bars = fetcher(
                self.symbol,
                "5m",
                daily_bar.open_time,
                end_time,
                required,
            )
        except Exception as exc:
            message = " ".join(str(exc).split())[:300]
            self._log(
                "ERROR",
                f"历史 K 线回补失败，将继续等待实时行情：{message}",
            )
            return

        for historical_bar in bars:
            if self.stop_event.is_set():
                return
            # Historical signals are intentionally discarded: the bars only seed
            # indicator state and must never cause a retroactive order.
            self.strategy.on_bar(historical_bar)
        loaded = int(getattr(self.strategy, "warmup_bars", 0))
        self._log(
            "INFO",
            f"历史 K 线回补完成，预热 {loaded}/{required}",
        )

    def _preload_futures_warmup(self) -> None:
        if self.config.app.provider != "binance_futures":
            return
        # The legacy automatic-direction path needs its current daily candle
        # before the strategy accepts intraday bars. It keeps using the daily
        # callback backfill below; the normal UI uses a locked manual direction.
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

    def _sync_trade_count(self) -> None:
        day_key = getattr(self.strategy, "current_day_key", None)
        restore = getattr(self.strategy, "restore_trade_count", None)
        if day_key is None or not callable(restore):
            return
        restore(day_key, self.ledger.count_consumed(self.symbol, day_key))

    def _update_risk_cache(self, *, paper: bool) -> None:
        position = self.ledger.position_summary(self.symbol, paper=paper)
        self._position_quantity = position.quantity
        self._average_entry_price = position.average_price
        self._pending_orders = self.ledger.pending_count(
            self.symbol, paper=paper
        )
        day_key = getattr(self.strategy, "current_day_key", None)
        self._daily_buy_notional = (
            self.ledger.daily_buy_notional(day_key, paper=paper)
            if day_key is not None
            else Decimal("0")
        )

    def _reconcile_orders(self) -> None:
        for record in self.ledger.unresolved_with_order_id(self.symbol):
            self._reconcile_record(record)
        self._last_order_reconcile_at = time.monotonic()

    def _reconcile_single_order(
        self,
        client_order_id: str,
        order_id: str,
    ) -> None:
        record = self.ledger.get_record(client_order_id)
        if record is None:
            self._log("ERROR", f"本地账本找不到订单 {order_id}")
            return
        self._reconcile_record(record)
        self._last_order_reconcile_at = time.monotonic()

    def _reconcile_record(self, record: OrderRecord) -> None:
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
            self.ledger.mark_lifecycle(
                record.client_order_id,
                status,
                f"从 Binance 同步为 {status}",
                filled_quantity=filled_quantity,
                average_price=average_price,
                fee=fee,
            )
            self._log("INFO", f"订单 {record.order_id} 状态已同步为 {status}")
        except ValueError as exc:
            self.ledger.mark_unknown(record.client_order_id, str(exc))
            self._log(
                "ERROR",
                f"订单 {record.order_id} 无法安全解析，已转为未知状态并锁定：{exc}",
            )
        except Exception as exc:
            self._log(
                "ERROR",
                f"无法同步订单 {record.order_id} 状态，继续锁定后续下单：{exc}",
            )

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
                trigger = f"当前价 {bar.close} <= 止损价 {stop_price}"
                label = "止损"
            elif bar.close >= take_price:
                trigger = f"当前价 {bar.close} >= 止盈价 {take_price}"
                label = "止盈"
            else:
                return None
        else:
            stop_price = average_price * (Decimal("1") + stop_percent)
            take_price = average_price * (Decimal("1") - take_percent)
            side = Side.BUY
            direction = "空头"
            if bar.close >= stop_price:
                trigger = f"当前价 {bar.close} >= 止损价 {stop_price}"
                label = "止损"
            elif bar.close <= take_price:
                trigger = f"当前价 {bar.close} <= 止盈价 {take_price}"
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
        trades_today = getattr(self.strategy, "trades_today", 0)
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
            self._snapshot.trades_today = trades_today
            self._snapshot.position_quantity = self._position_quantity
            self._snapshot.average_entry_price = self._average_entry_price
            self._snapshot.pending_orders = self._pending_orders
            self._snapshot.daily_buy_notional = self._daily_buy_notional
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

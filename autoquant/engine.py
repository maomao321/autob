from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Callable

from autoquant.config import AppConfig
from autoquant.models import (
    Bar,
    OrderRequest,
    RunState,
    RuntimeSnapshot,
    Side,
    Signal,
)
from autoquant.providers.base import TradingProvider
from autoquant.providers.binance_stocks import (
    BinanceStocksProvider,
    OrderRejectedError,
    OrderValidationError,
)
from autoquant.state import (
    OrderLedger,
    OrderRecord,
    PortfolioPerformance,
    RiskLimitError,
)
from autoquant.strategies.base import Strategy
from autoquant.strategies.five_minute_breakout import FiveMinuteBreakoutStrategy


SnapshotCallback = Callable[[RuntimeSnapshot], None]
LogCallback = Callable[[str, str, str], None]


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    app: AppConfig
    api_key: str = ""
    api_secret: str = ""


def create_provider(config: RunnerConfig) -> TradingProvider:
    if config.app.provider == "binance_stocks":
        return BinanceStocksProvider(
            api_key=config.api_key,
            api_secret=config.api_secret,
            live_trading=config.app.trading_mode == "REAL",
            rest_base_url=config.app.rest_base_url,
            websocket_base_url=config.app.websocket_base_url,
            recv_window=config.app.recv_window,
        )
    raise ValueError(f"未知 API 供应商: {config.app.provider}")


def create_strategy(symbol: str, config: RunnerConfig) -> Strategy:
    if config.app.strategy == "five_minute_breakout":
        return FiveMinuteBreakoutStrategy(
            symbol=symbol,
            ma_period=config.app.ma_period,
            max_trades_per_day=config.app.max_trades_per_day,
        )
    raise ValueError(f"未知策略: {config.app.strategy}")


class SymbolRunner:
    def __init__(
        self,
        symbol: str,
        config: RunnerConfig,
        snapshot_callback: SnapshotCallback,
        log_callback: LogCallback,
        ledger: OrderLedger | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.config = config
        self.provider = create_provider(config)
        self.strategy = create_strategy(self.symbol, config)
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

    @property
    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        if self.is_alive:
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name=f"autoquant-{self.symbol}",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        if not self.is_alive:
            self._update(RunState.STOPPED, "已停止")
            return
        self._update(RunState.STOPPING, "正在停止")
        self.stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        if self.thread:
            self.thread.join(timeout=timeout)

    def snapshot(self) -> RuntimeSnapshot:
        with self._snapshot_lock:
            return replace(self._snapshot)

    def _run(self) -> None:
        self._update(RunState.STARTING, "正在校验股票并连接行情")
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
                    f"股票校验通过，tradability={info.get('tradability', 'UNKNOWN')}",
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
            self._update(RunState.WARMING_UP, "等待日线和 5 分钟 K 线")
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
                signal = self.strategy.on_bar(bar)
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
                if signal.side is Side.BUY and position.quantity > 0 and not is_paper:
                    message = "未下单：当前已有程序管理的多头持仓，禁止重复加仓"
                    self._log("ERROR", message)
                    self._refresh_market_snapshot(message)
                    continue
                if (
                    not is_long_exit
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
                if self.config.app.trading_mode == "REAL":
                    if signal.side is Side.SELL and not is_long_exit:
                        message = (
                            "未下单：没有程序跟踪的多头持仓，"
                            "实盘 SELL 不会用于建立空头"
                        )
                        self._log("ERROR", message)
                        self._refresh_market_snapshot(message)
                        continue
                    current_info = self.provider.check_symbol(self.symbol)
                    if self.stop_event.is_set():
                        self._log("INFO", "停止请求已生效，股票校验后不再下单")
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
                    buy_notional=Decimal(self.config.app.buy_notional),
                    sell_quantity=(
                        position.quantity
                        if is_long_exit
                        else Decimal(self.config.app.sell_quantity)
                    ),
                    client_order_id=f"aq{uuid.uuid4().hex}",
                )
                if (
                    order.side is Side.BUY
                    and order.buy_notional
                    > Decimal(self.config.app.max_order_notional)
                ):
                    message = "资金风控阻止下单：买入金额超过单笔金额上限"
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
                            order.buy_notional / order.reference_price
                            if order.side is Side.BUY
                            else order.sell_quantity
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
        self._update(RunState.STOPPED, "已停止")
        self._log("INFO", "量化运行已停止")

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
            payload = self.provider.get_order_detail(record.order_id)
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
            if (
                record.side == Side.BUY.value
                and filled_quantity > 0
                and average_price <= 0
            ):
                raise ValueError("买入订单缺少成交均价，保持安全锁定")
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
        if quantity <= 0 or average_price <= 0:
            return None
        stop_price = average_price * (
            Decimal("1")
            - Decimal(self.config.app.stop_loss_percent) / Decimal("100")
        )
        take_price = average_price * (
            Decimal("1")
            + Decimal(self.config.app.take_profit_percent) / Decimal("100")
        )
        if bar.close <= stop_price:
            reason = (
                f"风险止损：当前价 {bar.close} <= 止损价 {stop_price}，"
                f"平掉 {quantity} 股多头"
            )
        elif bar.close >= take_price:
            reason = (
                f"风险止盈：当前价 {bar.close} >= 止盈价 {take_price}，"
                f"平掉 {quantity} 股多头"
            )
        else:
            return None
        return Signal(
            symbol=self.symbol,
            side=Side.SELL,
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
        ready = warmup_bars >= warmup_required and direction.value != "UNKNOWN"
        state = RunState.RUNNING if ready else RunState.WARMING_UP
        if message is None:
            if ready:
                message = f"运行中；日线方向 {direction.value}"
            else:
                message = f"预热 {warmup_bars}/{warmup_required}，等待日线方向"
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

    def stop(self, symbol: str) -> None:
        with self._lock:
            runner = self._runners.get(symbol.upper())
        if runner:
            runner.stop()

    def stop_all(self) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.stop()

    def join_all(self, timeout_per_runner: float = 2.0) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.join(timeout=timeout_per_runner)

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

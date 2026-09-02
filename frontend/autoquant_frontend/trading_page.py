from __future__ import annotations

import re
import threading
import time
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from autoquant_frontend.client import BackendClientError, RemoteRunnerConfig
from autoquant_frontend.constants import (
    ACTION_COLUMN,
    MANUAL_DIRECTION_COLUMN,
    MANUAL_DIRECTION_OPTIONS,
    REALIZED_PNL_COLUMN,
    STATE_TEXT,
    UNREALIZED_PNL_COLUMN,
)
from autoquant_frontend.dialogs import ask_yes_no, show_error, show_info
from autoquant_frontend.theme import COLORS
from autoquant_frontend.widgets import KeyedTable, TextValue
from autoquant_shared.config import AppConfig, MAX_SYMBOLS, normalize_symbols
from autoquant_shared.models import (
    AccountOverview,
    Direction,
    RunState,
    RuntimeSnapshot,
)


class TradingPageMixin:
    """Trading monitor, runner controls, configuration, and account overview."""

    def _build_main_page(self) -> None:
        layout = QVBoxLayout(self.main_page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        overview = QHBoxLayout()
        overview.setSpacing(10)
        self.account_total_label = self._metric_card(
            overview,
            "Binance 账户总金额",
            self.account_total_var,
            "Stocks 为 USDC；Futures 为 USDT",
        )
        self.realized_pnl_label = self._metric_card(
            overview,
            "已实现盈亏金额（程序）",
            self.realized_pnl_var,
            "已确认成交，包含已记录手续费",
        )
        self.unrealized_pnl_label = self._metric_card(
            overview,
            "未实现盈亏金额（程序）",
            self.unrealized_pnl_var,
            "程序持仓按最新买卖中间价估算",
        )
        actions = QFrame()
        actions.setObjectName("metricCard")
        actions.setStyleSheet(
            "QFrame#metricCard { background: white; border: 1px solid #d8dee9; border-radius: 8px; }"
        )
        action_layout = QVBoxLayout(actions)
        action_layout.addWidget(self._button("立即刷新", lambda: self._refresh_account_overview(manual=True), primary=True))
        action_layout.addWidget(self._button("打开运行配置", lambda: self.notebook.setCurrentWidget(self.config_page)))
        action_layout.addStretch()
        overview.addWidget(actions, 1)
        layout.addLayout(overview)

        status = self._bound_label(self.account_status_var, muted=True, wrap=True)
        layout.addWidget(status)

        controls = QHBoxLayout()
        controls.setSpacing(7)
        controls.addWidget(QLabel("标的代码"))
        symbol_entry = self._line(self.symbol_var)
        symbol_entry.setPlaceholderText("例如 AAPL, BTCUSDT")
        symbol_entry.setMaximumWidth(260)
        symbol_entry.returnPressed.connect(self._add_symbols)
        controls.addWidget(symbol_entry)
        controls.addWidget(self._button("添加", self._add_symbols, primary=True))
        controls.addWidget(self._button("移除", self._remove_selected))
        controls.addWidget(self._button("核对解锁", self._resolve_unknown_selected))
        controls.addStretch()
        self.start_selected_button = self._button(
            "启动", self._start_selected, primary=True
        )
        self.stop_selected_button = self._button("停止", self._stop_selected)
        self.start_all_button = self._button(
            "全部启动", self._start_all, primary=True
        )
        self.stop_all_button = self._button("全部停止", self._stop_all)
        for button in (
            self.start_selected_button,
            self.stop_selected_button,
            self.start_all_button,
            self.stop_all_button,
        ):
            button.hide()
            controls.addWidget(button)
        layout.addLayout(controls)

        headers = [
            "标的", "状态", "实际方向", "手动方向", "最新价", "已实现收益",
            "未实现收益", "程序持仓", "持仓均价", "未决订单",
            "开仓金额",
            "操作", "信息",
        ]
        widths = [80, 80, 85, 90, 90, 95, 95, 85, 85, 75, 120, 64, 300]
        self.tree = KeyedTable(headers, widths, multi_select=True)
        self.tree.setMinimumHeight(250)
        self.tree.verticalHeader().setDefaultSectionSize(40)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(5)
        log_layout.addWidget(QLabel("运行日志"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        log_layout.addWidget(self.log)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.tree)
        splitter.addWidget(log_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([430, 190])
        layout.addWidget(splitter, 1)

    def _metric_card(
        self, parent: QHBoxLayout, title: str, variable: TextValue, caption: str
    ) -> QLabel:
        card = QFrame()
        card.setObjectName("metricCard")
        card.setStyleSheet(
            "QFrame#metricCard { background: white; border: 1px solid #d8dee9; border-radius: 8px; }"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(3)
        card_layout.addWidget(QLabel(title))
        value = self._bound_label(variable)
        value_font = value.font()
        value_font.setPointSize(20)
        value_font.setWeight(QFont.Weight.Bold)
        value.setFont(value_font)
        value.setProperty("pnl", "neutral")
        card_layout.addWidget(value)
        subtitle = QLabel(caption)
        subtitle.setStyleSheet(f"color: {COLORS['muted']}; font-size: 11px;")
        card_layout.addWidget(subtitle)
        parent.addWidget(card, 2)
        return value

    def _current_config(self) -> AppConfig:
        manual_directions = {
            symbol: self.tree.combo_text(symbol, MANUAL_DIRECTION_COLUMN)
            for symbol in self.tree.get_children()
        }
        config = AppConfig(
            symbols=list(self.tree.get_children()),
            contract_pool=list(self.config.contract_pool),
            manual_directions=manual_directions,
            provider=self.provider_var.get(),
            leverage=self.leverage_var.get(),
            api_key=self.api_key_var.get().strip(),
            api_secret=self.api_secret_var.get().strip(),
            strategy=self.strategy_var.get(), trading_mode=self.mode_var.get(),
            ma_period=self.config.ma_period, buy_notional=self.buy_notional_var.get().strip(),
            max_additions_per_position=int(self.max_additions_var.get()),
            max_order_notional=self.max_order_notional_var.get().strip(),
            max_daily_buy_notional=self.max_daily_buy_notional_var.get().strip(),
            stop_loss_percent=self.stop_loss_var.get().strip(),
            take_profit_percent=self.take_profit_var.get().strip(),
            max_signal_age_seconds=int(self.max_signal_age_var.get()),
            ai_provider=(
                self.ai_provider_var.get()
                if self.ai_enabled_checkbox.isChecked()
                else "DISABLED"
            ), openai_model=self.openai_model_var.get().strip(),
            deepseek_model=self.deepseek_model_var.get().strip(),
            qwen_model=self.qwen_model_var.get().strip(),
            qwen_chat_url=self.qwen_chat_url_var.get().strip(),
            openai_reasoning_enabled=(
                self.openai_reasoning_checkbox.isChecked()
            ),
            openai_reasoning_effort=(
                self.openai_reasoning_effort_var.get().strip()
            ),
            deepseek_thinking_enabled=(
                self.deepseek_thinking_checkbox.isChecked()
            ),
            deepseek_reasoning_effort=(
                self.deepseek_reasoning_effort_var.get().strip()
            ),
            qwen_thinking_enabled=(
                self.qwen_reasoning_checkbox.isChecked()
            ),
            qwen_reasoning_effort=(
                self.qwen_reasoning_effort_var.get().strip()
            ),
            openai_api_key=self.openai_api_key_var.get().strip(),
            deepseek_api_key=self.deepseek_api_key_var.get().strip(),
            qwen_api_key=self.qwen_api_key_var.get().strip(),
            ai_min_confidence=self.ai_min_confidence_var.get().strip(),
            ai_history_days=int(self.ai_history_days_var.get()),
            ai_entry_timing_bars=int(self.ai_entry_timing_bars_var.get()),
            ai_news_days=int(self.ai_news_days_var.get()),
            ai_news_limit=int(self.ai_news_limit_var.get()),
            ai_timeout_seconds=int(self.ai_timeout_var.get()),
            rest_base_url=self.config.rest_base_url,
            websocket_base_url=self.config.websocket_base_url,
            recv_window=self.config.recv_window,
        )
        config.validate()
        return config

    def _runner_config(self) -> RemoteRunnerConfig:
        app = self._current_config()
        openai_api_key = self.openai_api_key_var.get().strip()
        deepseek_api_key = self.deepseek_api_key_var.get().strip()
        qwen_api_key = self.qwen_api_key_var.get().strip()
        return RemoteRunnerConfig(
            app=app, api_key=self.api_key_var.get(), api_secret=self.api_secret_var.get(),
            openai_api_key=openai_api_key, deepseek_api_key=deepseek_api_key,
            qwen_api_key=qwen_api_key,
        )

    def _insert_symbol(self, symbol: str) -> None:
        symbol = symbol.upper()
        if self.tree.exists(symbol):
            return
        self.tree.insert(
            "", None, iid=symbol, text=symbol,
            values=("已停止", "FLAT", "FLAT", "-", "0.00", "0.00", "0.00", "-", "0", "0", "", "未启动"),
        )
        self.tree.set_combo(
            symbol,
            MANUAL_DIRECTION_COLUMN,
            MANUAL_DIRECTION_OPTIONS,
            self.config.manual_directions.get(symbol, "FLAT"),
            tooltip=(
                "大模型开关关闭时，选择 LONG、SHORT 或 FLAT 作为开仓方向。"
                "大模型开关开启时忽略此值。启动后不可修改。"
            ),
        )
        self.tree.set_action_button(
            symbol,
            ACTION_COLUMN,
            lambda _checked=False, symbol=symbol: self._start_symbols([symbol]),
            lambda _checked=False, symbol=symbol: self._stop_symbols([symbol]),
        )

    def _manual_direction(self, symbol: str) -> Direction:
        value = self.tree.combo_text(symbol, MANUAL_DIRECTION_COLUMN)
        return Direction(value)

    def _add_symbols(self) -> None:
        try:
            raw_symbols = re.split(r"[,，;；\s]+", self.symbol_var.get())
            if self.provider_var.get().strip().lower() == "binance_futures":
                raw_symbols = [
                    symbol
                    if symbol.strip().upper().endswith("USDT")
                    else f"{symbol}USDT"
                    for symbol in raw_symbols
                    if symbol.strip()
                ]
            new_symbols = normalize_symbols(raw_symbols)
            visible_symbols = list(self.tree.get_children())
            visible_set = set(visible_symbols)
            symbols_to_insert = [
                symbol for symbol in new_symbols if symbol not in visible_set
            ]
            combined = visible_set | set(symbols_to_insert)
            if len(combined) > MAX_SYMBOLS:
                raise ValueError(f"标的数量不能超过 {MAX_SYMBOLS} 个")

            persisted_symbols = normalize_symbols(
                [*self.config.symbols, *symbols_to_insert]
            )
            if persisted_symbols != self.config.symbols:
                updated_config = replace(
                    self.config,
                    symbols=persisted_symbols,
                )
                self.store.save(updated_config)
                self.config = updated_config

            for symbol in symbols_to_insert:
                self._insert_symbol(symbol)
            self.symbol_var.set("")
        except (BackendClientError, OSError, ValueError, TypeError) as exc:
            show_error("添加标的失败", str(exc))

    def _remove_selected(self) -> None:
        selected = list(self.tree.selection())
        if not selected:
            return
        try:
            stop_targets = self.controller.stop_targets(selected)
        except Exception as exc:
            show_error("后端不可用", str(exc))
            return
        if stop_targets:
            show_info(
                "请先停止并平仓",
                "运行中或仍有程序持仓的标的不能直接移除。请先使用“停止所选并平仓”。",
            )
            return

        confirmed = ask_yes_no(
            "确认移除标的",
            f"即将从交易监控和服务器配置中移除：{', '.join(selected)}。\n\n"
            "历史交易记录不会被删除。确认继续吗？",
        )
        if not confirmed:
            return

        selected_set = set(selected)
        updated_config = replace(
            self.config,
            symbols=[
                symbol
                for symbol in self.config.symbols
                if symbol not in selected_set
            ],
            manual_directions={
                symbol: direction
                for symbol, direction in self.config.manual_directions.items()
                if symbol not in selected_set
            },
        )
        try:
            self.store.save(updated_config)
        except (BackendClientError, OSError, ValueError, TypeError) as exc:
            show_error("移除标的失败", str(exc))
            return

        self.config = updated_config
        for symbol in selected:
            self.tree.delete(symbol)

    def _resolve_unknown_selected(self) -> None:
        selected = self._selected_symbols()
        if not selected:
            return
        try:
            locked = [
                symbol
                for symbol in selected
                if self.controller.unknown_live_orders(symbol) > 0
            ]
        except Exception as exc:
            show_error("后端不可用", str(exc))
            return
        if not locked:
            show_info("没有锁定", "所选股票没有未知实盘订单。")
            return
        confirmed = ask_yes_no(
            "确认已经人工核对",
            "只有在你已经登录 Binance，确认所有未知订单的成交状态，并处理了对应持仓后才能解除。\n\n"
            f"即将解除：{', '.join(locked)}\n\n确认已经完成核对吗？",
        )
        if not confirmed:
            return
        try:
            total = sum(self.controller.resolve_unknown_live_orders(symbol) for symbol in locked)
            show_info("已解除", f"已归档 {total} 笔未知订单记录。")
        except RuntimeError as exc:
            show_error("无法解除", str(exc))

    def _selected_symbols(self) -> list[str]:
        selected = list(self.tree.selection())
        if not selected:
            show_info("请选择股票", "请先在列表中选择至少一只股票。")
        return selected

    def _symbol_context_menu(self) -> QMenu:
        menu = QMenu(self.tree)
        start_action = menu.addAction("启动")
        stop_action = menu.addAction("停止")
        menu.addSeparator()
        remove_action = menu.addAction("移除")
        start_action.triggered.connect(
            lambda _checked=False: self._start_selected()
        )
        stop_action.triggered.connect(
            lambda _checked=False: self._stop_selected()
        )
        remove_action.triggered.connect(
            lambda _checked=False: self._remove_selected()
        )
        return menu

    def _show_symbol_context_menu(self, position: QPoint) -> None:
        index = self.tree.indexAt(position)
        if not index.isValid():
            return
        if index.row() not in {
            selected.row() for selected in self.tree.selectedIndexes()
        }:
            self.tree.clearSelection()
            self.tree.selectRow(index.row())
        menu = self._symbol_context_menu()
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _start_selected(self) -> None:
        self._start_symbols(self._selected_symbols())

    def _start_all(self) -> None:
        self._start_symbols(list(self.tree.get_children()))

    def _start_symbols(self, symbols: list[str]) -> None:
        if not symbols:
            return
        try:
            config = self._runner_config()
        except (ValueError, TypeError) as exc:
            show_error("配置错误", str(exc))
            return
        if config.app.trading_mode == "REAL" and not self._confirm_real_mode(config.app):
            return
        try:
            for symbol in symbols:
                direction = (
                    Direction.UNKNOWN
                    if config.app.ai_provider != "DISABLED"
                    else self._manual_direction(symbol)
                )
                self.controller.start(
                    symbol,
                    replace(config, manual_direction=direction),
                )
        except Exception as exc:
            show_error("启动失败", str(exc))

    def _stop_selected(self) -> None:
        self._stop_symbols(self._selected_symbols())

    def _stop_all(self) -> None:
        self._stop_symbols(list(self.tree.get_children()))

    def _stop_symbols(self, symbols: list[str]) -> None:
        if not symbols:
            return
        try:
            targets = self.controller.stop_targets(symbols)
        except Exception as exc:
            show_error("后端不可用", str(exc))
            return
        if not targets:
            show_info("无需停止", "所选标的均已停止且没有程序持仓。")
            return
        details = "\n".join(
            f"{symbol}：{mode}，{self._position_label(quantity)}"
            for symbol, mode, quantity in targets
        )
        real_positions = [
            f"{symbol}（MARKET {'SELL' if quantity > 0 else 'BUY'}）"
            for symbol, mode, quantity in targets
            if mode == "REAL" and quantity != 0
        ]
        warning = (
            "\n\n警告：以下 REAL 持仓将向 Binance 提交真实平仓单："
            + ", ".join(real_positions)
            if real_positions
            else ""
        )
        confirmed = ask_yes_no(
            "确认停止并强制平仓",
            "程序会先阻止新的策略订单，再按本地账本记录的全部净持仓数量平仓。"
            "没有持仓时只停止策略；有未知或未决订单时会拒绝自动平仓。\n\n"
            f"{details}{warning}\n\n确认继续吗？",
        )
        if not confirmed:
            return
        try:
            for symbol, _mode, _quantity in targets:
                self.controller.stop(symbol, close_position=True)
        except Exception as exc:
            show_error("停止失败", str(exc))

    def _confirm_real_mode(self, config: AppConfig) -> bool:
        if not self.api_key_var.get().strip() or not self.api_secret_var.get().strip():
            show_error("缺少凭据", "REAL 模式必须填写 API Key 和 API Secret。")
            return False
        if config.provider == "binance_futures":
            provider_detail = (
                f"；杠杆：{config.leverage}x。\n"
                "Futures 账户必须使用单向持仓模式；LONG 只开多、SHORT 只开空，"
                "持仓未平时禁止反向或重复开仓。"
            )
            direction_detail = "退出单只会减掉程序确认的当前方向持仓。"
        else:
            provider_detail = (
                "。\n账户还必须已经接受 Binance 美股交易免责声明。"
            )
            direction_detail = "Stocks 不建立空头，SELL 只会平掉程序确认的多头。"
        ai_detail = (
            f"大模型：{config.ai_provider}，将审核今日方向和候选入场时机。"
            if config.ai_provider != "DISABLED"
            else "大模型：已禁用，使用表格手动方向。"
        )
        return ask_yes_no(
            "确认真实交易",
            "当前为 REAL 模式，策略信号会向 Binance 提交真实 MARKET 订单。\n\n"
            f"标的数：{len(self.tree.get_children())}；单笔名义金额：{config.buy_notional}。\n"
            f"每日账户上限：{config.max_daily_buy_notional}{provider_detail}\n"
            f"止损/止盈：{self.stop_loss_var.get()}% / {self.take_profit_var.get()}%。"
            f"{direction_detail}\n{ai_detail}\n\n确认继续吗？",
        )

    def _save_config(self) -> None:
        try:
            self.config = self._current_config()
            self.store.save(self.config)
            show_info("已保存", f"配置已保存到:\n{self.store.path}")
            self._refresh_account_overview(manual=True)
        except (BackendClientError, OSError, ValueError, TypeError) as exc:
            show_error("保存失败", str(exc))

    def _check_connection(self) -> None:
        try:
            runner_config = self._runner_config()
        except (ValueError, TypeError) as exc:
            show_error("配置错误", str(exc))
            return
        symbols = list(self.tree.selection()) or list(self.tree.get_children())[:1]
        if not symbols:
            show_info("请添加标的", "请先添加至少一个交易标的。")
            return
        symbol = symbols[0]
        self._append_log("INFO", symbol, "正在检查 Binance API 与标的代码")

        def check() -> None:
            try:
                message = self.controller.check_connection(symbol, runner_config)
                self._enqueue_event(("dialog", "info", "连接检查", message))
                self._enqueue_event(("log", "INFO", symbol, message))
                self._enqueue_event(("account_refresh",))
            except Exception as exc:
                self._enqueue_event(("dialog", "error", "连接失败", str(exc)))
                self._enqueue_event(("log", "ERROR", symbol, str(exc)))

        threading.Thread(target=check, name="api-check", daemon=True).start()

    def _account_runner_config(self) -> RemoteRunnerConfig:
        account_config = replace(
            self.config,
            trading_mode=self.mode_var.get().strip().upper(),
            api_key=self.api_key_var.get().strip(),
            api_secret=self.api_secret_var.get().strip(),
        )
        account_config.validate()
        return RemoteRunnerConfig(
            app=account_config, api_key=self.api_key_var.get().strip(),
            api_secret=self.api_secret_var.get().strip(),
        )

    def _account_refresh_tick(self) -> None:
        if not self._closed:
            self._refresh_account_overview()

    def _refresh_account_overview(self, manual: bool = False) -> None:
        if self._account_refresh_inflight:
            if manual:
                self.account_status_var.set("账户数据正在刷新，请稍候")
            return
        try:
            runner_config = self._account_runner_config()
        except (TypeError, ValueError) as exc:
            self.account_status_var.set(f"账户概览配置无效：{exc}")
            return
        prices = dict(self._latest_prices)
        self._account_refresh_inflight = True
        self.account_status_var.set("正在通过后端刷新 Binance 钱包余额和程序盈亏…")

        def refresh() -> None:
            try:
                overview = self.controller.account_overview(runner_config, prices)
            except Exception as exc:
                overview = AccountOverview(
                    message=f"后端账户概览刷新失败：{exc}",
                    updated_at=int(time.time() * 1000),
                )
            self._enqueue_event(("account", overview))

        threading.Thread(target=refresh, name="account-overview-refresh", daemon=True).start()

    def _apply_account_overview(self, overview: AccountOverview) -> None:
        self._account_refresh_inflight = False
        self.account_total_var.set(
            "不可用" if overview.total_balance is None else f"{overview.total_balance:,.2f} {overview.currency}"
        )
        self._set_pnl_value(self.realized_pnl_var, self.realized_pnl_label, overview.realized_pnl, overview.currency)
        self._set_pnl_value(self.unrealized_pnl_var, self.unrealized_pnl_label, overview.unrealized_pnl, overview.currency)
        detail = overview.message
        if overview.missing_price_symbols:
            detail += "；缺少持仓报价：" + ", ".join(overview.missing_price_symbols)
        timestamp = datetime.fromtimestamp(overview.updated_at / 1000).strftime("%H:%M:%S")
        self.account_status_var.set(f"{detail}；更新时间 {timestamp}")

    @staticmethod
    def _set_pnl_value(variable: TextValue, label: QLabel, value: Decimal | None, currency: str) -> None:
        if value is None:
            variable.set("行情不可用")
            color = COLORS["text"]
        else:
            variable.set(f"{value:+,.2f} {currency}")
            color = COLORS["positive"] if value > 0 else COLORS["negative"] if value < 0 else COLORS["text"]
        label.setStyleSheet(f"color: {color};")
    def _apply_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        if not self.tree.exists(snapshot.symbol):
            return
        if snapshot.last_price is not None and snapshot.last_price > 0:
            self._latest_prices[snapshot.symbol] = snapshot.last_price
        tag = "error" if snapshot.state is RunState.ERROR else "running" if snapshot.state is RunState.RUNNING else "signal" if snapshot.state is RunState.SIGNAL else ""
        manual_direction = self.tree.combo_text(
            snapshot.symbol, MANUAL_DIRECTION_COLUMN
        )
        values = (
            STATE_TEXT[snapshot.state], snapshot.direction.value, manual_direction,
            self._format_decimal(snapshot.last_price, 2),
            self._format_decimal(snapshot.realized_pnl, 2),
            self._format_decimal(snapshot.unrealized_pnl, 2),
            self._format_decimal(snapshot.position_quantity, 2), self._format_decimal(snapshot.average_entry_price, 2),
            str(snapshot.pending_orders),
            self._format_decimal(snapshot.session_open_notional, 2),
            "", snapshot.message,
        )
        self.tree.item_update(snapshot.symbol, values=values, tags=(tag,) if tag else ())
        for column, pnl in (
            (REALIZED_PNL_COLUMN, snapshot.realized_pnl),
            (UNREALIZED_PNL_COLUMN, snapshot.unrealized_pnl),
        ):
            if pnl is None:
                continue
            pnl_color = (
                COLORS["positive"]
                if pnl > 0
                else COLORS["negative"]
                if pnl < 0
                else COLORS["text"]
            )
            self.tree.set_cell_foreground(snapshot.symbol, column, pnl_color)
        self.tree.set_combo_enabled(
            snapshot.symbol,
            MANUAL_DIRECTION_COLUMN,
            snapshot.state in {RunState.STOPPED, RunState.ERROR},
        )
        self.tree.set_action_state(
            snapshot.symbol,
            action=(
                "start"
                if snapshot.state in {RunState.STOPPED, RunState.ERROR}
                else "stop"
            ),
            enabled=snapshot.state is not RunState.STOPPING,
        )

    def _append_log(self, level: str, symbol: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{timestamp}] [{level}] [{symbol}] {message}")
        document = self.log.document()
        if document.blockCount() > 5000:
            cursor = self.log.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            for _ in range(document.blockCount() - 5000):
                cursor.select(cursor.SelectionType.BlockUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)

    @staticmethod
    def _format_decimal(value: object | None, places: int | None = None) -> str:
        if value is None:
            return "-"
        return format(value, "f" if places is None else f".{places}f")

    @staticmethod
    def _position_label(quantity: Decimal) -> str:
        if quantity > 0:
            return f"程序多头 {quantity}"
        if quantity < 0:
            return f"程序空头 {abs(quantity)}"
        return "无程序持仓"

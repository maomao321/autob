from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from autoquant.config import (
    MAX_SYMBOLS,
    AppConfig,
    ConfigStore,
    credential_or_environment,
    normalize_symbols,
)
from autoquant.client import (
    BackendClient,
    BackendClientError,
    RemoteConfigStore,
    RemoteRunnerConfig,
    RemoteTradingController,
)
from autoquant.experience import (
    ExperienceError,
    ExperienceImportResult,
    OpenAIVectorStoreUploader,
    TradeExperience,
    UploadResult,
    default_experience_path,
    import_external_experiences,
    merge_experience_document,
    summarize_experiences,
    write_experience_document,
)
from autoquant.models import AccountOverview, Direction, RunState, RuntimeSnapshot


ACCOUNT_REFRESH_MS = 30_000
MANUAL_DIRECTION_COLUMN = 3
MANUAL_DIRECTION_OPTIONS = ("LONG", "SHORT", "FLAT")

STATE_TEXT = {
    RunState.STOPPED: "已停止",
    RunState.STARTING: "启动中",
    RunState.WARMING_UP: "收集K线",
    RunState.RUNNING: "运行中",
    RunState.SIGNAL: "信号",
    RunState.ERROR: "错误",
    RunState.STOPPING: "停止中",
}

COLORS = {
    "text": "#172033",
    "muted": "#667085",
    "border": "#d8dee9",
    "surface": "#ffffff",
    "canvas": "#f5f7fb",
    "primary": "#1769e0",
    "primary_hover": "#0f5ecf",
    "positive": "#087830",
    "negative": "#b42318",
    "warning": "#9a5b00",
    "signal": "#0856a8",
}


class TextValue:
    """A tiny Qt-friendly replacement for Tk's StringVar."""

    def __init__(self, value: str = "") -> None:
        self._value = str(value)
        self._writers: list[Callable[[str], None]] = []

    def get(self) -> str:
        return self._value

    def set(self, value: object = "") -> None:
        text = str(value)
        self._value = text
        for writer in tuple(self._writers):
            writer(text)

    def bind_line_edit(self, widget: QLineEdit) -> None:
        widget.setText(self._value)
        widget.textChanged.connect(self._from_widget)
        self._writers.append(lambda text: self._set_line_text(widget, text))

    def bind_combo(self, widget: QComboBox) -> None:
        widget.setCurrentText(self._value)
        widget.currentTextChanged.connect(self._from_widget)
        self._writers.append(lambda text: self._set_combo_text(widget, text))

    def bind_label(self, widget: QLabel) -> None:
        widget.setText(self._value)
        self._writers.append(widget.setText)

    def _from_widget(self, text: str) -> None:
        self._value = text

    @staticmethod
    def _set_line_text(widget: QLineEdit, text: str) -> None:
        if widget.text() != text:
            widget.setText(text)

    @staticmethod
    def _set_combo_text(widget: QComboBox, text: str) -> None:
        if widget.currentText() != text:
            widget.setCurrentText(text)


class KeyedTable(QTableWidget):
    """QTableWidget with stable string row IDs used by the trading controller."""

    TAG_COLORS = {
        "error": COLORS["negative"],
        "running": COLORS["positive"],
        "signal": COLORS["signal"],
        "win": COLORS["positive"],
        "loss": COLORS["negative"],
    }

    def __init__(
        self,
        headers: list[str],
        widths: list[int],
        *,
        multi_select: bool,
    ) -> None:
        super().__init__(0, len(headers))
        self._keys: list[str] = []
        self.setHorizontalHeaderLabels(headers)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setSelectionMode(
            QTableWidget.SelectionMode.ExtendedSelection
            if multi_select
            else QTableWidget.SelectionMode.SingleSelection
        )
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSortingEnabled(False)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setHighlightSections(False)
        self.horizontalHeader().setStretchLastSection(True)
        for index, width in enumerate(widths):
            self.setColumnWidth(index, width)
        self.horizontalHeader().setSectionResizeMode(
            len(headers) - 1, QHeaderView.ResizeMode.Stretch
        )

    def get_children(self) -> tuple[str, ...]:
        return tuple(self._keys)

    def exists(self, key: str) -> bool:
        return key in self._keys

    def selection(self) -> tuple[str, ...]:
        rows = sorted({index.row() for index in self.selectedIndexes()})
        return tuple(self._keys[row] for row in rows if row < len(self._keys))

    def insert(
        self,
        _parent: str,
        _position: object,
        *,
        iid: str,
        text: str,
        values: tuple[object, ...],
        tags: tuple[str, ...] = (),
    ) -> None:
        if self.exists(iid):
            return
        row = self.rowCount()
        self.insertRow(row)
        self._keys.append(iid)
        self._write_row(row, (text, *values), tags)

    def delete(self, key: str) -> None:
        if key not in self._keys:
            return
        row = self._keys.index(key)
        self.removeRow(row)
        self._keys.pop(row)

    def item_update(
        self,
        key: str,
        *,
        values: tuple[object, ...],
        tags: tuple[str, ...] = (),
    ) -> None:
        if key not in self._keys:
            return
        row = self._keys.index(key)
        symbol_item = self.item(row, 0)
        symbol = symbol_item.text() if symbol_item else key
        self._write_row(row, (symbol, *values), tags)

    def set_combo(
        self,
        key: str,
        column: int,
        options: tuple[str, ...],
        current: str,
        *,
        tooltip: str = "",
    ) -> QComboBox:
        if key not in self._keys:
            raise KeyError(key)
        combo = QComboBox(self)
        combo.addItems(options)
        combo.setCurrentText(current if current in options else options[0])
        combo.setToolTip(tooltip)
        self.setCellWidget(self._keys.index(key), column, combo)
        return combo

    def combo_text(self, key: str, column: int) -> str:
        if key not in self._keys:
            raise KeyError(key)
        widget = self.cellWidget(self._keys.index(key), column)
        if not isinstance(widget, QComboBox):
            raise ValueError(f"{key} 第 {column} 列不是下拉框")
        return widget.currentText()

    def set_combo_enabled(self, key: str, column: int, enabled: bool) -> None:
        if key not in self._keys:
            return
        widget = self.cellWidget(self._keys.index(key), column)
        if isinstance(widget, QComboBox):
            widget.setEnabled(enabled)

    def clear_rows(self) -> None:
        self.setRowCount(0)
        self._keys.clear()

    def _write_row(
        self, row: int, values: tuple[object, ...], tags: tuple[str, ...]
    ) -> None:
        foreground = QColor(self.TAG_COLORS.get(tags[0], COLORS["text"])) if tags else None
        for column, value in enumerate(values):
            item = self.item(row, column)
            is_new = item is None
            if item is None:
                item = QTableWidgetItem()
            item.setText(str(value))
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | (
                    Qt.AlignmentFlag.AlignLeft
                    if column == self.columnCount() - 1
                    else Qt.AlignmentFlag.AlignHCenter
                )
            )
            if foreground is not None:
                item.setForeground(foreground)
            else:
                item.setForeground(QColor(COLORS["text"]))
            if is_new:
                self.setItem(row, column, item)


def _message_parent() -> QWidget | None:
    return QApplication.activeWindow()


def show_info(title: str, message: str) -> None:
    QMessageBox.information(_message_parent(), title, message)


def show_error(title: str, message: str) -> None:
    QMessageBox.critical(_message_parent(), title, message)


def show_warning(title: str, message: str) -> None:
    QMessageBox.warning(_message_parent(), title, message)


def ask_yes_no(title: str, message: str) -> bool:
    result = QMessageBox.question(
        _message_parent(),
        title,
        message,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return result == QMessageBox.StandardButton.Yes


class AutoQuantApp(QMainWindow):
    def __init__(
        self,
        config_store: ConfigStore | RemoteConfigStore | None = None,
        backend_client: BackendClient | None = None,
        controller: RemoteTradingController | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("AutoQuant - Binance Stocks 量化控制台")
        self.resize(1280, 820)
        self.setMinimumSize(1020, 680)
        self.backend_client = backend_client or BackendClient()
        self.store = config_store or RemoteConfigStore(self.backend_client)
        self.events: queue.Queue[tuple] = queue.Queue(maxsize=1000)
        self.config = self._load_config()

        self.provider_var = TextValue(self.config.provider)
        self.leverage_var = TextValue(str(self.config.leverage))
        self.strategy_var = TextValue(self.config.strategy)
        self.mode_var = TextValue(self.config.trading_mode)
        self.api_key_var = TextValue(
            credential_or_environment(self.config.api_key, "BINANCE_API_KEY")
        )
        self.api_secret_var = TextValue(
            credential_or_environment(self.config.api_secret, "BINANCE_API_SECRET")
        )
        self.ma_var = TextValue(str(self.config.ma_period))
        self.buy_notional_var = TextValue(self.config.buy_notional)
        self.sell_quantity_var = TextValue(self.config.sell_quantity)
        self.max_trades_var = TextValue(str(self.config.max_trades_per_day))
        self.max_order_notional_var = TextValue(self.config.max_order_notional)
        self.max_daily_buy_notional_var = TextValue(
            self.config.max_daily_buy_notional
        )
        self.stop_loss_var = TextValue(self.config.stop_loss_percent)
        self.take_profit_var = TextValue(self.config.take_profit_percent)
        self.max_signal_age_var = TextValue(str(self.config.max_signal_age_seconds))
        self.ai_provider_var = TextValue("DISABLED")
        self.openai_model_var = TextValue(self.config.openai_model)
        self.deepseek_model_var = TextValue(self.config.deepseek_model)
        self.openai_api_key_var = TextValue(os.environ.get("OPENAI_API_KEY", ""))
        self.deepseek_api_key_var = TextValue(os.environ.get("DEEPSEEK_API_KEY", ""))
        self.ai_min_confidence_var = TextValue(self.config.ai_min_confidence)
        self.ai_history_days_var = TextValue(str(self.config.ai_history_days))
        self.ai_news_days_var = TextValue(str(self.config.ai_news_days))
        self.ai_news_limit_var = TextValue(str(self.config.ai_news_limit))
        self.ai_timeout_var = TextValue(str(self.config.ai_timeout_seconds))
        self.experience_trade_path_var = TextValue()
        self.experience_kline_path_var = TextValue()
        self.experience_pattern_bars_var = TextValue("20")
        self.experience_vector_store_var = TextValue()
        self.experience_summary_var = TextValue("尚未导入交易经验")
        self.experience_status_var = TextValue(
            "长期经验将保存到本地；只有点击上传后才会发送到 OpenAI。"
        )
        self.symbol_var = TextValue()
        self.account_total_var = TextValue("—")
        self.realized_pnl_var = TextValue("0.00 USDC")
        self.unrealized_pnl_var = TextValue("0.00 USDC")
        self.account_status_var = TextValue("等待首次刷新")

        self._experiences: list[TradeExperience] = []
        self._experience_extract_inflight = False
        self._experience_upload_inflight = False
        self._latest_prices: dict[str, Decimal] = {}
        self._account_refresh_inflight = False
        self._closed = False

        self.controller = controller or RemoteTradingController(
            self.backend_client,
            snapshot_callback=lambda snapshot: self._enqueue_event(("snapshot", snapshot)),
            log_callback=lambda level, symbol, message: self._enqueue_event(
                ("log", level, symbol, message)
            ),
        )
        self._build_ui()
        for symbol in self.config.symbols:
            self._insert_symbol(symbol)

        self.event_timer = QTimer(self)
        self.event_timer.timeout.connect(self._drain_events)
        self.event_timer.start(100)
        self.account_timer = QTimer(self)
        self.account_timer.timeout.connect(self._account_refresh_tick)
        self.account_timer.start(ACCOUNT_REFRESH_MS)
        QTimer.singleShot(500, self._account_refresh_tick)

    def _load_config(self) -> AppConfig:
        try:
            return self.store.load()
        except Exception as exc:
            message = str(exc)
            QTimer.singleShot(0, lambda: show_warning("配置警告", message))
            return AppConfig()

    def _build_ui(self) -> None:
        self.setStyleSheet(self._style_sheet())
        self.notebook = QTabWidget()
        self.notebook.setDocumentMode(True)
        self.setCentralWidget(self.notebook)
        self.main_page = QWidget()
        self.config_page = QWidget()
        self.experience_page = QWidget()
        self.notebook.addTab(self.main_page, "交易监控")
        self.notebook.addTab(self.config_page, "运行配置")
        self.notebook.addTab(self.experience_page, "交易经验库")
        self._build_main_page()
        self._build_config_page()
        self._build_experience_page()

    @staticmethod
    def _style_sheet() -> str:
        return f"""
            QMainWindow, QWidget {{ color: {COLORS['text']}; font-size: 13px; }}
            QMainWindow {{ background: {COLORS['canvas']}; }}
            QTabWidget::pane {{ border: 0; background: {COLORS['canvas']}; }}
            QTabBar::tab {{ padding: 11px 22px; color: {COLORS['muted']}; }}
            QTabBar::tab:selected {{ color: {COLORS['primary']}; border-bottom: 2px solid {COLORS['primary']}; }}
            QGroupBox {{ background: {COLORS['surface']}; border: 1px solid {COLORS['border']}; border-radius: 8px; margin-top: 12px; padding-top: 12px; font-weight: 600; }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 5px; }}
            QLineEdit, QComboBox {{ min-height: 30px; border: 1px solid {COLORS['border']}; border-radius: 5px; padding: 0 8px; background: white; }}
            QLineEdit:focus, QComboBox:focus {{ border-color: {COLORS['primary']}; }}
            QPushButton {{ min-height: 31px; padding: 0 13px; border: 1px solid {COLORS['border']}; border-radius: 5px; background: white; }}
            QPushButton:hover {{ border-color: {COLORS['primary']}; color: {COLORS['primary']}; }}
            QPushButton[primary="true"] {{ color: white; background: {COLORS['primary']}; border-color: {COLORS['primary']}; font-weight: 600; }}
            QPushButton[primary="true"]:hover {{ background: {COLORS['primary_hover']}; }}
            QPushButton:disabled {{ color: #98a2b3; background: #f2f4f7; }}
            QTableWidget {{ background: white; border: 1px solid {COLORS['border']}; border-radius: 7px; gridline-color: #edf0f5; alternate-background-color: #f8fafc; selection-background-color: #e8f1ff; selection-color: {COLORS['text']}; }}
            QHeaderView::section {{ background: #f0f3f8; border: 0; border-bottom: 1px solid {COLORS['border']}; padding: 8px 5px; font-weight: 600; }}
            QTextEdit {{ background: #101828; color: #d0d5dd; border: 0; border-radius: 7px; padding: 8px; font-family: Menlo, Consolas, monospace; }}
            QScrollArea {{ border: 0; background: {COLORS['canvas']}; }}
        """

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
        controls.addWidget(self._button("移除所选", self._remove_selected))
        controls.addWidget(self._button("核对后解除未知订单锁", self._resolve_unknown_selected))
        controls.addStretch()
        controls.addWidget(self._button("启动所选", self._start_selected, primary=True))
        controls.addWidget(self._button("停止所选并平仓", self._stop_selected))
        controls.addWidget(self._button("全部启动", self._start_all, primary=True))
        controls.addWidget(self._button("全部停止并平仓", self._stop_all))
        layout.addLayout(controls)

        headers = [
            "标的", "状态", "实际方向", "手动方向", "最新价", "MA", "实时K线", "今日交易",
            "程序持仓", "持仓均价", "未决订单", "今日开仓额", "信息",
        ]
        widths = [80, 80, 85, 90, 90, 90, 75, 80, 85, 85, 75, 90, 300]
        self.tree = KeyedTable(headers, widths, multi_select=True)
        self.tree.setMinimumHeight(250)

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

    def _build_config_page(self) -> None:
        outer = QVBoxLayout(self.config_page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(14, 8, 14, 14)
        content_layout.setSpacing(10)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        settings = QGroupBox("运行配置")
        grid = QGridLayout(settings)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        for column in (1, 3, 5, 7):
            grid.setColumnStretch(column, 1)

        self._grid_field(
            grid,
            0,
            0,
            "API 供应商",
            self._combo(self.provider_var, ["binance_stocks", "binance_futures"]),
        )
        self._grid_field(grid, 0, 2, "量化策略", self._combo(self.strategy_var, ["five_minute_breakout"]))
        self._grid_field(grid, 0, 4, "交易模式", self._combo(self.mode_var, ["PAPER", "REAL"]))
        save_button = self._button("保存配置", self._save_config, primary=True)
        grid.addWidget(save_button, 0, 6, 1, 2)

        self._grid_field(grid, 1, 0, "API Key", self._line(self.api_key_var), span=2)
        self._grid_field(grid, 1, 3, "API Secret", self._line(self.api_secret_var, secret=True), span=2)
        grid.addWidget(self._button("检查 API 与标的", self._check_connection), 1, 6, 1, 2)

        self._grid_field(grid, 2, 0, "MA 周期", self._line(self.ma_var))
        self._grid_field(grid, 2, 2, "开仓金额(USDC/USDT)", self._line(self.buy_notional_var))
        self._grid_field(grid, 2, 4, "卖出数量", self._line(self.sell_quantity_var))
        self._grid_field(grid, 2, 6, "每日最多交易", self._line(self.max_trades_var))
        self._grid_field(grid, 3, 0, "单笔上限(USDC/USDT)", self._line(self.max_order_notional_var))
        self._grid_field(grid, 3, 2, "每日开仓上限", self._line(self.max_daily_buy_notional_var))
        risk = QWidget()
        risk_layout = QHBoxLayout(risk)
        risk_layout.setContentsMargins(0, 0, 0, 0)
        risk_layout.addWidget(self._line(self.stop_loss_var))
        risk_layout.addWidget(QLabel("/"))
        risk_layout.addWidget(self._line(self.take_profit_var))
        self._grid_field(grid, 3, 4, "止损/止盈(%)", risk)
        self._grid_field(grid, 3, 6, "信号有效期(秒)", self._line(self.max_signal_age_var))
        self._grid_field(grid, 4, 0, "Futures 杠杆倍数", self._line(self.leverage_var))
        warning = QLabel(
            "默认 PAPER 只记录模拟订单。REAL 会真实下单；Stocks 只支持做多，"
            "Futures 支持做多和做空，但仅支持单向持仓，持仓期间禁止反向或重复开仓。"
            "Futures 实盘下单前设置所选杠杆。“停止并平仓”会减掉全部程序持仓；"
            "未知订单会锁定实盘。"
            "API Key/Secret 会保存到后端服务器，请保护服务器配置和访问令牌。"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {COLORS['warning']};")
        grid.addWidget(warning, 5, 0, 1, 8)
        content_layout.addWidget(settings)

        ai_settings = QGroupBox("交易经验库上传")
        ai_grid = QGridLayout(ai_settings)
        ai_grid.setHorizontalSpacing(12)
        ai_grid.setVerticalSpacing(10)
        ai_grid.setColumnStretch(1, 1)
        self._grid_field(
            ai_grid,
            0,
            0,
            "OpenAI API Key",
            self._line(self.openai_api_key_var, secret=True),
        )
        ai_note = QLabel(
            "仅在“交易经验”页点击上传时使用；不会参与开仓方向判断，也不会写入配置文件。"
        )
        ai_note.setWordWrap(True)
        ai_note.setStyleSheet(f"color: {COLORS['muted']};")
        ai_grid.addWidget(ai_note, 1, 0, 1, 2)
        content_layout.addWidget(ai_settings)
        content_layout.addStretch()

    def _build_experience_page(self) -> None:
        layout = QVBoxLayout(self.experience_page)
        layout.setContentsMargins(14, 8, 14, 14)
        layout.setSpacing(8)
        source = QGroupBox("1. 导入外部交易经验")
        source_grid = QGridLayout(source)
        source_grid.setColumnStretch(1, 1)
        source_grid.addWidget(QLabel("交易记录 Excel/CSV（可选）"), 0, 0)
        source_grid.addWidget(self._line(self.experience_trade_path_var), 0, 1)
        source_grid.addWidget(self._button("选择文件", self._browse_experience_trade_file), 0, 2)
        source_grid.addWidget(QLabel("K线形态 Excel/CSV（可选）"), 1, 0)
        source_grid.addWidget(self._line(self.experience_kline_path_var), 1, 1)
        source_grid.addWidget(self._button("选择文件", self._browse_experience_kline_file), 1, 2)
        pattern_row = QHBoxLayout()
        pattern_row.addWidget(QLabel("每个形态最多K线数"))
        bars = self._line(self.experience_pattern_bars_var)
        bars.setMaximumWidth(100)
        pattern_row.addWidget(bars)
        pattern_row.addStretch()
        self.experience_extract_button = self._button("导入并预览", self._extract_experience_records, primary=True)
        pattern_row.addWidget(self.experience_extract_button)
        source_grid.addLayout(pattern_row, 2, 0, 1, 3)
        source_note = QLabel(
            "两类文件均可单独导入，也可用相同 trade_id / pattern_id 关联。交易记录需包含标的、开平仓时间/价格和数量；"
            "K线需包含时间与 OHLC。不会读取本程序订单账本，且关联交易时只采用开仓前已收盘K线。"
        )
        source_note.setWordWrap(True)
        source_note.setStyleSheet(f"color: {COLORS['muted']};")
        source_grid.addWidget(source_note, 3, 0, 1, 3)
        layout.addWidget(source)

        summary_row = QHBoxLayout()
        summary = self._bound_label(self.experience_summary_var)
        summary_font = summary.font()
        summary_font.setPointSize(12)
        summary_font.setWeight(QFont.Weight.Bold)
        summary.setFont(summary_font)
        summary_row.addWidget(summary)
        summary_row.addStretch()
        bias_note = QLabel("盈利和亏损样本会一起上传，避免幸存者偏差。")
        bias_note.setStyleSheet(f"color: {COLORS['warning']};")
        summary_row.addWidget(bias_note)
        layout.addLayout(summary_row)

        headers = ["股票", "结果", "来源", "开始时间(UTC)", "数量", "入场/起始价", "出场/结束价", "净盈亏", "收益率(%)", "持有时间", "K线形态"]
        widths = [75, 65, 75, 165, 80, 90, 90, 85, 85, 90, 210]
        self.experience_tree = KeyedTable(headers, widths, multi_select=False)
        layout.addWidget(self.experience_tree, 1)

        upload = QGroupBox("2. 保存或上传知识库")
        upload_grid = QGridLayout(upload)
        upload_grid.setColumnStretch(1, 1)
        upload_grid.addWidget(QLabel("本地共享经验库"), 0, 0)
        path_label = QLabel(str(default_experience_path()))
        path_label.setStyleSheet(f"color: {COLORS['muted']};")
        upload_grid.addWidget(path_label, 0, 1)
        upload_grid.addWidget(self._button("保存到本地", self._save_local_experience_library), 0, 2)
        upload_grid.addWidget(self._button("另存为 JSON", self._export_experience_records), 0, 3)
        upload_grid.addWidget(QLabel("OpenAI Vector Store ID"), 1, 0)
        upload_grid.addWidget(self._line(self.experience_vector_store_var), 1, 1)
        self.experience_upload_button = self._button("上传到 OpenAI", self._upload_experience_records, primary=True)
        upload_grid.addWidget(self.experience_upload_button, 1, 2, 1, 2)
        upload_note = QLabel("ID 留空会创建新的 Vector Store；已有 ID 则追加文件。DeepSeek 暂无本页托管上传目标，后续由本地共享经验库检索后提供给它。")
        upload_note.setWordWrap(True)
        upload_note.setStyleSheet(f"color: {COLORS['muted']};")
        upload_grid.addWidget(upload_note, 2, 0, 1, 4)
        upload_grid.addWidget(self._bound_label(self.experience_status_var, color=COLORS["signal"], wrap=True), 3, 0, 1, 4)
        layout.addWidget(upload)

    @staticmethod
    def _grid_field(
        layout: QGridLayout,
        row: int,
        column: int,
        title: str,
        widget: QWidget,
        *,
        span: int = 1,
    ) -> None:
        layout.addWidget(QLabel(title), row, column)
        layout.addWidget(widget, row, column + 1, 1, span)

    @staticmethod
    def _button(text: str, callback: Callable[[], None], *, primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("primary", primary)
        button.clicked.connect(callback)
        return button

    @staticmethod
    def _line(variable: TextValue, *, secret: bool = False) -> QLineEdit:
        widget = QLineEdit()
        if secret:
            widget.setEchoMode(QLineEdit.EchoMode.Password)
        variable.bind_line_edit(widget)
        return widget

    @staticmethod
    def _combo(variable: TextValue, values: list[str]) -> QComboBox:
        widget = QComboBox()
        widget.addItems(values)
        variable.bind_combo(widget)
        return widget

    @staticmethod
    def _bound_label(
        variable: TextValue,
        *,
        muted: bool = False,
        color: str | None = None,
        wrap: bool = False,
    ) -> QLabel:
        widget = QLabel()
        variable.bind_label(widget)
        if wrap:
            widget.setWordWrap(True)
        chosen = color or (COLORS["muted"] if muted else None)
        if chosen:
            widget.setStyleSheet(f"color: {chosen};")
        return widget

    @staticmethod
    def _ask_experience_file(title: str) -> str:
        selected, _ = QFileDialog.getOpenFileName(
            _message_parent(), title, "", "Excel 或 CSV (*.xlsx *.csv);;Excel 工作簿 (*.xlsx);;CSV 文件 (*.csv);;所有文件 (*)"
        )
        return selected

    def _browse_experience_trade_file(self) -> None:
        selected = self._ask_experience_file("选择外部交易记录")
        if selected:
            self.experience_trade_path_var.set(selected)

    def _browse_experience_kline_file(self) -> None:
        selected = self._ask_experience_file("选择外部K线形态")
        if selected:
            self.experience_kline_path_var.set(selected)

    def _extract_experience_records(self) -> None:
        if self._experience_extract_inflight:
            return
        try:
            pattern_bars = int(self.experience_pattern_bars_var.get())
            if not 5 <= pattern_bars <= 240:
                raise ValueError("每个形态的K线数量必须在 5 到 240 之间")
            trade_text = self.experience_trade_path_var.get().strip()
            kline_text = self.experience_kline_path_var.get().strip()
            trade_path = Path(trade_text) if trade_text else None
            kline_path = Path(kline_text) if kline_text else None
            if trade_path is None and kline_path is None:
                raise ValueError("请至少选择一个交易记录或K线形态文件")
            for label, path in (("交易记录", trade_path), ("K线形态", kline_path)):
                if path is None:
                    continue
                if not path.is_file():
                    raise ValueError(f"选择的{label}文件不存在")
                if path.suffix.lower() not in {".xlsx", ".csv"}:
                    raise ValueError(f"{label}只支持 .xlsx 或 .csv 文件")
        except ValueError as exc:
            show_error("导入配置错误", str(exc))
            return

        self._experience_extract_inflight = True
        self.experience_extract_button.setEnabled(False)
        self.experience_status_var.set("正在读取外部交易记录和K线形态……")

        def extract() -> None:
            try:
                result = import_external_experiences(
                    trade_path=trade_path, kline_path=kline_path, pattern_bars=pattern_bars
                )
                self._enqueue_event(("experience_extracted", result))
            except Exception as exc:
                self._enqueue_event(("experience_error", "extract", str(exc) or exc.__class__.__name__))

        threading.Thread(target=extract, name="experience-extractor", daemon=True).start()

    def _apply_experience_records(self, result: ExperienceImportResult) -> None:
        self._experience_extract_inflight = False
        self.experience_extract_button.setEnabled(True)
        experiences = result.experiences
        self._experiences = experiences
        self.experience_tree.clear_rows()
        outcome_text = {"WIN": "盈利", "LOSS": "亏损", "BREAKEVEN": "持平", "UNLABELED": "形态"}
        for index, item in enumerate(experiences):
            pattern = item.pre_entry_pattern
            kline = (
                f"{pattern.get('bar_count', 0)}根 / {pattern.get('shape_signature', '未分类')}"
                if pattern.get("available") else "未提供"
            )
            tag = "win" if item.outcome == "WIN" else "loss" if item.outcome == "LOSS" else ""
            self.experience_tree.insert(
                "", None, iid=f"experience-{index}", text=item.symbol,
                values=(
                    outcome_text.get(item.outcome, item.outcome),
                    "交易记录" if item.record_type == "TRADE" else "K线形态",
                    item.entry_time.replace("+00:00", "Z"), item.quantity,
                    item.entry_price, item.exit_price, item.net_pnl,
                    item.return_percent, self._format_holding_seconds(item.holding_seconds), kline,
                ),
                tags=(tag,) if tag else (),
            )
        summary = summarize_experiences(experiences)
        self.experience_summary_var.set(
            f"经验 {summary.total} 条｜交易 {summary.trades}｜形态 {summary.patterns}｜"
            f"盈利 {summary.wins}｜亏损 {summary.losses}｜持平 {summary.breakeven}｜"
            f"含K线 {summary.with_kline}｜交易净盈亏 {summary.net_pnl}"
        )
        if experiences:
            detail = f"已导入 {result.trade_rows} 条交易记录"
            if result.kline_rows:
                detail += f"和 {result.kline_rows} 根K线"
            self.experience_status_var.set(detail + "；请核对后保存或上传。")
        else:
            self.experience_status_var.set("文件已读取，但没有可导入的交易记录或K线形态。")

    def _save_local_experience_library(self) -> None:
        if not self._experiences:
            show_info("没有经验", "请先从外部文件导入至少一条经验。")
            return
        try:
            path, added, total = merge_experience_document(default_experience_path(), self._experiences)
            self.experience_status_var.set(f"已保存本地经验库：新增 {added} 笔，共 {total} 笔；{path}")
            show_info("保存完成", f"本地经验库共 {total} 笔：\n{path}")
        except ExperienceError as exc:
            show_error("保存失败", str(exc))

    def _export_experience_records(self) -> None:
        if not self._experiences:
            show_info("没有经验", "请先从外部文件导入至少一条经验。")
            return
        selected, _ = QFileDialog.getSaveFileName(
            self, "导出交易经验", "external_trade_experiences.json", "JSON 文件 (*.json)"
        )
        if not selected:
            return
        if not selected.lower().endswith(".json"):
            selected += ".json"
        try:
            path = write_experience_document(Path(selected), self._experiences)
            self.experience_status_var.set(f"已导出 {len(self._experiences)} 笔：{path}")
        except ExperienceError as exc:
            show_error("导出失败", str(exc))

    def _upload_experience_records(self) -> None:
        if self._experience_upload_inflight:
            return
        if not self._experiences:
            show_info("没有经验", "请先从外部文件导入至少一条经验。")
            return
        api_key = self.openai_api_key_var.get().strip()
        if not api_key:
            show_error("缺少凭据", "请先在“运行配置”页填写 OpenAI API Key。")
            return
        vector_store_id = self.experience_vector_store_var.get().strip()
        confirmed = ask_yes_no(
            "确认上传交易数据",
            "将把本次外部数据合并到本地经验库，并把合并后的股票代码、成交价格、"
            "盈亏、持有时间和K线形态上传到 OpenAI Vector Store。API Key 不会写入文件。\n\n"
            f"本次导入记录：{len(self._experiences)} 条。确认继续吗？",
        )
        if not confirmed:
            return
        try:
            timeout_seconds = max(5, min(120, int(self.ai_timeout_var.get())))
        except ValueError:
            timeout_seconds = 30
        self._experience_upload_inflight = True
        self.experience_upload_button.setEnabled(False)
        self.experience_status_var.set("正在保存本地经验库并上传到 OpenAI……")

        def upload() -> None:
            try:
                path, added, total = merge_experience_document(default_experience_path(), self._experiences)
                result = OpenAIVectorStoreUploader().upload(
                    path, api_key=api_key, vector_store_id=vector_store_id,
                    timeout_seconds=timeout_seconds,
                )
                self._enqueue_event(("experience_uploaded", result, added, total, str(path)))
            except Exception as exc:
                self._enqueue_event(("experience_error", "upload", str(exc) or exc.__class__.__name__))

        threading.Thread(target=upload, name="experience-uploader", daemon=True).start()

    def _apply_experience_upload(self, result: UploadResult, added: int, total: int, path: str) -> None:
        self._experience_upload_inflight = False
        self.experience_upload_button.setEnabled(True)
        self.experience_vector_store_var.set(result.vector_store_id)
        self.experience_status_var.set(
            f"上传已受理：Vector Store {result.vector_store_id}，文件 {result.file_id}，索引状态 {result.status}。"
        )
        show_info(
            "上传已受理",
            f"本地库新增 {added} 笔，共 {total} 笔：\n{path}\n\nVector Store：{result.vector_store_id}\n"
            f"文件：{result.file_id}\n状态：{result.status}",
        )

    def _apply_experience_error(self, operation: str, message: str) -> None:
        if operation == "extract":
            self._experience_extract_inflight = False
            self.experience_extract_button.setEnabled(True)
            title = "经验导入失败"
        else:
            self._experience_upload_inflight = False
            self.experience_upload_button.setEnabled(True)
            title = "经验上传失败"
        self.experience_status_var.set(message)
        show_error(title, message)

    @staticmethod
    def _format_holding_seconds(seconds: int) -> str:
        hours, remainder = divmod(max(0, seconds), 3600)
        minutes, remaining_seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}时{minutes}分"
        if minutes:
            return f"{minutes}分{remaining_seconds}秒"
        return f"{remaining_seconds}秒"

    def _current_config(self) -> AppConfig:
        manual_directions = {
            symbol: self.tree.combo_text(symbol, MANUAL_DIRECTION_COLUMN)
            for symbol in self.tree.get_children()
        }
        config = AppConfig(
            symbols=list(self.tree.get_children()),
            manual_directions=manual_directions,
            provider=self.provider_var.get(),
            leverage=self.leverage_var.get(),
            api_key=self.api_key_var.get().strip(),
            api_secret=self.api_secret_var.get().strip(),
            strategy=self.strategy_var.get(), trading_mode=self.mode_var.get(),
            ma_period=int(self.ma_var.get()), buy_notional=self.buy_notional_var.get().strip(),
            sell_quantity=self.sell_quantity_var.get().strip(),
            max_trades_per_day=int(self.max_trades_var.get()),
            max_order_notional=self.max_order_notional_var.get().strip(),
            max_daily_buy_notional=self.max_daily_buy_notional_var.get().strip(),
            stop_loss_percent=self.stop_loss_var.get().strip(),
            take_profit_percent=self.take_profit_var.get().strip(),
            max_signal_age_seconds=int(self.max_signal_age_var.get()),
            ai_provider="DISABLED", openai_model=self.openai_model_var.get().strip(),
            deepseek_model=self.deepseek_model_var.get().strip(),
            ai_min_confidence=self.ai_min_confidence_var.get().strip(),
            ai_history_days=int(self.ai_history_days_var.get()),
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
        return RemoteRunnerConfig(
            app=app, api_key=self.api_key_var.get(), api_secret=self.api_secret_var.get(),
            openai_api_key=openai_api_key, deepseek_api_key=deepseek_api_key,
        )

    def _insert_symbol(self, symbol: str) -> None:
        symbol = symbol.upper()
        if self.tree.exists(symbol):
            return
        self.tree.insert(
            "", None, iid=symbol, text=symbol,
            values=("已停止", "FLAT", "FLAT", "-", "-", "0/0", "0", "0", "-", "0", "0", "未启动"),
        )
        self.tree.set_combo(
            symbol,
            MANUAL_DIRECTION_COLUMN,
            MANUAL_DIRECTION_OPTIONS,
            self.config.manual_directions.get(symbol, "FLAT"),
            tooltip=(
                "选择 LONG、SHORT 或 FLAT 作为唯一开仓方向。"
                "启动后不可修改。"
            ),
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
                self.controller.start(
                    symbol,
                    replace(config, manual_direction=self._manual_direction(symbol)),
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
        return ask_yes_no(
            "确认真实交易",
            "当前为 REAL 模式，策略信号会向 Binance 提交真实 MARKET 订单。\n\n"
            f"标的数：{len(self.tree.get_children())}；单笔名义金额：{config.buy_notional}。\n"
            f"每日账户上限：{config.max_daily_buy_notional}{provider_detail}\n"
            f"止损/止盈：{self.stop_loss_var.get()}% / {self.take_profit_var.get()}%。"
            f"{direction_detail}\n\n确认继续吗？",
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

    def _enqueue_event(self, event: tuple) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(event)
            except queue.Full:
                pass

    def _drain_events(self) -> None:
        for _ in range(200):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "snapshot":
                self._apply_snapshot(event[1])
            elif event[0] == "log":
                self._append_log(event[1], event[2], event[3])
            elif event[0] == "dialog":
                _kind, severity, title, message = event
                (show_error if severity == "error" else show_info)(title, message)
            elif event[0] == "account":
                self._apply_account_overview(event[1])
            elif event[0] == "account_refresh":
                self._refresh_account_overview(manual=True)
            elif event[0] == "experience_extracted":
                self._apply_experience_records(event[1])
            elif event[0] == "experience_uploaded":
                self._apply_experience_upload(event[1], event[2], event[3], event[4])
            elif event[0] == "experience_error":
                self._apply_experience_error(event[1], event[2])
        if not self.events.empty():
            QTimer.singleShot(10, self._drain_events)

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
            self._format_decimal(snapshot.last_price), self._format_decimal(snapshot.ma_value),
            f"{snapshot.warmup_bars}/{snapshot.warmup_required}", str(snapshot.trades_today),
            self._format_decimal(snapshot.position_quantity), self._format_decimal(snapshot.average_entry_price),
            str(snapshot.pending_orders), self._format_decimal(snapshot.daily_buy_notional), snapshot.message,
        )
        self.tree.item_update(snapshot.symbol, values=values, tags=(tag,) if tag else ())
        self.tree.set_combo_enabled(
            snapshot.symbol,
            MANUAL_DIRECTION_COLUMN,
            snapshot.state in {RunState.STOPPED, RunState.ERROR},
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
    def _format_decimal(value: object | None) -> str:
        return "-" if value is None else format(value, "f")

    @staticmethod
    def _position_label(quantity: Decimal) -> str:
        if quantity > 0:
            return f"程序多头 {quantity}"
        if quantity < 0:
            return f"程序空头 {abs(quantity)}"
        return "无程序持仓"

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closed = True
        self.event_timer.stop()
        self.account_timer.stop()
        self.controller.close()
        event.accept()


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AutoQuant")
    app.setOrganizationName("AutoQuant")
    window = AutoQuantApp()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()

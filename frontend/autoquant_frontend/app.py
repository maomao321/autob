from __future__ import annotations

import json
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

from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QCursor,
    QFont,
    QIcon,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtCharts import (
    QChart,
    QChartView,
    QLineSeries,
    QScatterSeries,
    QValueAxis,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from autoquant_shared.config import (
    MAX_CONTRACT_POOL_SYMBOLS,
    MAX_SYMBOLS,
    SECRET_SENTINEL,
    AppConfig,
    ConfigStore,
    credential_or_environment,
    normalize_symbols,
)
from autoquant_frontend.client import (
    BackendClient,
    BackendClientError,
    RemoteConfigStore,
    RemoteRunnerConfig,
    RemoteTradingController,
)
from autoquant_frontend.experience import (
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
from autoquant_shared.models import (
    AccountOverview,
    AiDecisionHistoryItem,
    Direction,
    RunState,
    RuntimeSnapshot,
    TradeHistoryItem,
)


ACCOUNT_REFRESH_MS = 30_000
FUTURES_RANKINGS_REFRESH_MS = 30 * 60 * 1_000
CONTRACT_POOL_REFRESH_MS = 60 * 1_000
MANUAL_DIRECTION_COLUMN = 3
REALIZED_PNL_COLUMN = 5
UNREALIZED_PNL_COLUMN = 6
ACTION_COLUMN = 11
MANUAL_DIRECTION_OPTIONS = ("LONG", "SHORT", "FLAT")


def application_icon_path() -> Path:
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return bundle_root / "assets" / "autoquant-icon.png"
    return (
        Path(__file__).resolve().parents[2]
        / "packaging"
        / "assets"
        / "autoquant-icon.png"
    )


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


class InteractiveChartView(QChartView):
    """Chart view that resolves any plot-area mouse position to chart values."""

    def __init__(self, chart: QChart) -> None:
        super().__init__(chart)
        self._point_callback: Callable[[QPointF, bool], None] | None = None
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def set_point_callback(
        self, callback: Callable[[QPointF, bool], None]
    ) -> None:
        self._point_callback = callback

    def _dispatch_chart_position(
        self, position: QPointF, *, clicked: bool
    ) -> None:
        if (
            self._point_callback is None
            or not self.chart().plotArea().contains(position)
        ):
            return
        self._point_callback(self.chart().mapToValue(position), clicked)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._dispatch_chart_position(event.position(), clicked=False)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() is Qt.MouseButton.LeftButton:
            self._dispatch_chart_position(event.position(), clicked=True)
        super().mousePressEvent(event)


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
        self._action_buttons: dict[str, QPushButton] = {}
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
        self._action_buttons.pop(key, None)

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

    def set_action_button(
        self,
        key: str,
        column: int,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
    ) -> QPushButton:
        if key not in self._keys:
            raise KeyError(key)
        container = QWidget(self)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch()

        button = QPushButton(container)
        button.setObjectName("rowActionButton")
        button.setFixedSize(42, 34)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        def dispatch(_checked: bool = False) -> None:
            if button.property("action") == "stop":
                on_stop()
            else:
                on_start()

        button.clicked.connect(dispatch)
        layout.addWidget(button)
        layout.addStretch()
        self.setCellWidget(self._keys.index(key), column, container)
        self._action_buttons[key] = button
        self.set_action_state(key, action="start")
        return button

    def set_action_state(
        self, key: str, *, action: str, enabled: bool = True
    ) -> None:
        button = self._action_buttons.get(key)
        if button is None:
            return
        is_stop = action == "stop"
        color = COLORS["negative"] if is_stop else COLORS["positive"]
        hover_background = "#fdecea" if is_stop else "#e8f5ec"
        pressed_background = "#fbd5d1" if is_stop else "#d5eddd"
        verb = "停止并平仓" if is_stop else "启动"
        button.setProperty("action", "stop" if is_stop else "start")
        button.setText("●" if is_stop else "▶")
        button.setAccessibleName(f"{verb} {key}")
        button.setToolTip(f"{verb} {key}")
        button.setStyleSheet(
            f"""
            QPushButton#rowActionButton {{
                border: none;
                background: transparent;
                color: {color};
                padding: 0;
                font-size: 26px;
                font-weight: 700;
            }}
            QPushButton#rowActionButton:hover {{
                border: none;
                border-radius: 5px;
                background: {hover_background};
                color: {color};
            }}
            QPushButton#rowActionButton:pressed {{
                border: none;
                border-radius: 5px;
                background: {pressed_background};
                color: {color};
            }}
            QPushButton#rowActionButton:disabled {{
                border: none;
                background: transparent;
                color: {color};
            }}
            """
        )
        button.setEnabled(enabled)

    def action_button(self, key: str) -> QPushButton:
        if key not in self._action_buttons:
            raise KeyError(key)
        return self._action_buttons[key]

    def set_cell_foreground(self, key: str, column: int, color: str) -> None:
        if key not in self._keys:
            return
        item = self.item(self._keys.index(key), column)
        if item is not None:
            item.setForeground(QColor(color))

    def clear_rows(self) -> None:
        self.setRowCount(0)
        self._keys.clear()
        self._action_buttons.clear()

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
        self.setWindowIcon(QIcon(str(application_icon_path())))
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
        self.ai_provider_var = TextValue(
            self.config.ai_provider
            if self.config.ai_provider != "DISABLED"
            else "CHATGPT"
        )
        self.openai_model_var = TextValue(self.config.openai_model)
        self.deepseek_model_var = TextValue(self.config.deepseek_model)
        self.qwen_model_var = TextValue(self.config.qwen_model)
        self.qwen_chat_url_var = TextValue(self.config.qwen_chat_url)
        self.openai_reasoning_effort_var = TextValue(
            self.config.openai_reasoning_effort
        )
        self.deepseek_reasoning_effort_var = TextValue(
            self.config.deepseek_reasoning_effort
        )
        self.qwen_reasoning_effort_var = TextValue(
            self.config.qwen_reasoning_effort
        )
        self.openai_api_key_var = TextValue(
            credential_or_environment(
                self.config.openai_api_key, "OPENAI_API_KEY"
            )
        )
        self.deepseek_api_key_var = TextValue(
            credential_or_environment(
                self.config.deepseek_api_key, "DEEPSEEK_API_KEY"
            )
        )
        self.qwen_api_key_var = TextValue(
            credential_or_environment(
                self.config.qwen_api_key, "DASHSCOPE_API_KEY"
            )
        )
        self.ai_min_confidence_var = TextValue(self.config.ai_min_confidence)
        self.ai_history_days_var = TextValue(str(self.config.ai_history_days))
        self.ai_entry_timing_bars_var = TextValue(
            str(self.config.ai_entry_timing_bars)
        )
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
        self.trade_history_symbol_var = TextValue()
        self.trade_history_action_var = TextValue("全部")
        self.trade_history_mode_var = TextValue("全部")
        self.trade_history_limit_var = TextValue("500")
        self.trade_history_status_var = TextValue("请选择条件后点击查询")
        self.ai_decision_symbol_var = TextValue()
        self.ai_decision_stage_var = TextValue("全部")
        self.ai_decision_limit_var = TextValue("100")
        self.ai_decision_status_var = TextValue("点击查询查看持久化 AI 决策")
        self.backtest_symbol_var = TextValue(
            self.config.symbols[0] if self.config.symbols else ""
        )
        self.backtest_strategy_var = TextValue(self.config.strategy)
        self.backtest_status_var = TextValue(
            "下载最近 180 天范围内可用的日线、5 分钟和 1 分钟 K 线，再执行策略回测。"
        )
        self.contract_pool_status_var = TextValue(
            "打开页面后获取 Binance USDT 永续合约 24 小时涨跌榜，之后每 30 分钟自动刷新。"
        )

        self._experiences: list[TradeExperience] = []
        self._experience_extract_inflight = False
        self._experience_upload_inflight = False
        self._latest_prices: dict[str, Decimal] = {}
        self._account_refresh_inflight = False
        self._trade_history_inflight = False
        self._ai_decision_inflight = False
        self._backtest_refresh_inflight = False
        self._backtest_action_inflight = False
        self._backtest_detail_inflight = False
        self._backtest_downloads: dict[str, dict[str, object]] = {}
        self._backtest_detail_dialog: QDialog | None = None
        self._ai_decisions: dict[str, AiDecisionHistoryItem] = {}
        self._futures_tickers: dict[str, dict[str, str]] = {}
        self._futures_rankings_inflight = False
        self._futures_rankings_loaded = False
        self._contract_pool_refresh_inflight = False
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
        self.backtest_timer = QTimer(self)
        self.backtest_timer.timeout.connect(self._refresh_backtest_data)
        self.backtest_timer.start(5_000)
        self.futures_rankings_timer = QTimer(self)
        self.futures_rankings_timer.timeout.connect(
            self._auto_refresh_futures_rankings
        )
        self.futures_rankings_timer.start(FUTURES_RANKINGS_REFRESH_MS)
        self.contract_pool_timer = QTimer(self)
        self.contract_pool_timer.timeout.connect(
            self._auto_refresh_contract_pool
        )
        self.contract_pool_timer.setInterval(CONTRACT_POOL_REFRESH_MS)
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
        self.contract_pool_page = QWidget()
        self.config_page = QWidget()
        self.trade_history_page = QWidget()
        self.ai_decision_page = QWidget()
        self.experience_page = QWidget()
        self.backtest_page = QWidget()
        self.notebook.addTab(self.main_page, "交易监控")
        self.notebook.addTab(self.contract_pool_page, "合约池")
        self.notebook.addTab(self.config_page, "运行配置")
        self.notebook.addTab(self.trade_history_page, "交易记录")
        self.notebook.addTab(self.ai_decision_page, "AI 决策")
        self.notebook.addTab(self.experience_page, "交易经验库")
        self.notebook.addTab(self.backtest_page, "策略回测")
        self._build_main_page()
        self._build_contract_pool_page()
        self._build_config_page()
        self._build_trade_history_page()
        self._build_ai_decision_page()
        self._build_experience_page()
        self._build_backtest_page()
        self.notebook.currentChanged.connect(self._on_page_changed)

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

    def _build_contract_pool_page(self) -> None:
        layout = QVBoxLayout(self.contract_pool_page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        controls = QHBoxLayout()
        controls.addStretch()
        self.contract_pool_refresh_button = self._button(
            "刷新涨跌榜", self._refresh_futures_rankings
        )
        controls.addWidget(self.contract_pool_refresh_button)
        layout.addLayout(controls)

        content_splitter = QSplitter(Qt.Orientation.Horizontal)
        pool_group = QGroupBox("合约池")
        pool_layout = QVBoxLayout(pool_group)
        self.contract_pool_tree = KeyedTable(
            ["合约", "24h 涨跌幅"], [150, 120], multi_select=True
        )
        self.contract_pool_tree.verticalHeader().setDefaultSectionSize(34)
        self.contract_pool_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.contract_pool_tree.customContextMenuRequested.connect(
            self._show_contract_pool_context_menu
        )
        pool_layout.addWidget(self.contract_pool_tree)
        content_splitter.addWidget(pool_group)

        self.futures_ranking_tabs = QTabWidget()
        self.futures_ranking_tabs.setDocumentMode(True)
        gainers_page = QWidget()
        gainers_layout = QVBoxLayout(gainers_page)
        gainers_layout.setContentsMargins(8, 8, 8, 8)
        self.futures_gainers_tree = KeyedTable(
            ["合约", "24h 涨跌幅", "最新价", "24h 成交额(USDT)"],
            [110, 110, 120, 160],
            multi_select=True,
        )
        self.futures_gainers_tree.verticalHeader().setDefaultSectionSize(34)
        self.futures_gainers_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.futures_gainers_tree.customContextMenuRequested.connect(
            self._show_gainers_context_menu
        )
        gainers_layout.addWidget(self.futures_gainers_tree)
        self.futures_ranking_tabs.addTab(gainers_page, "涨幅榜")

        losers_page = QWidget()
        losers_layout = QVBoxLayout(losers_page)
        losers_layout.setContentsMargins(8, 8, 8, 8)
        self.futures_losers_tree = KeyedTable(
            ["合约", "24h 涨跌幅", "最新价", "24h 成交额(USDT)"],
            [110, 110, 120, 160],
            multi_select=True,
        )
        self.futures_losers_tree.verticalHeader().setDefaultSectionSize(34)
        self.futures_losers_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.futures_losers_tree.customContextMenuRequested.connect(
            self._show_losers_context_menu
        )
        losers_layout.addWidget(self.futures_losers_tree)
        self.futures_ranking_tabs.addTab(losers_page, "跌幅榜")
        content_splitter.addWidget(self.futures_ranking_tabs)
        content_splitter.setStretchFactor(0, 1)
        content_splitter.setStretchFactor(1, 3)
        content_splitter.setSizes([300, 900])
        layout.addWidget(content_splitter, 1)
        self._sync_contract_pool_table()

    def _on_page_changed(self, _index: int) -> None:
        if self.notebook.currentWidget() is self.contract_pool_page:
            self.contract_pool_timer.start(CONTRACT_POOL_REFRESH_MS)
            self._refresh_futures_rankings()
        else:
            self.contract_pool_timer.stop()

    def _auto_refresh_futures_rankings(self) -> None:
        if self.notebook.currentWidget() is self.contract_pool_page:
            self._refresh_futures_rankings()

    def _auto_refresh_contract_pool(self) -> None:
        if (
            self.notebook.currentWidget() is self.contract_pool_page
            and self._futures_rankings_loaded
            and self.config.contract_pool
        ):
            self._refresh_contract_pool_tickers()

    def _refresh_contract_pool_tickers(self) -> None:
        if (
            self._closed
            or self._contract_pool_refresh_inflight
            or self._futures_rankings_inflight
            or not self.config.contract_pool
        ):
            return
        self._contract_pool_refresh_inflight = True

        def load() -> None:
            try:
                payload = self.backend_client.futures_rankings(limit=1)
                tickers = payload.get("tickers", {})
                self._enqueue_event(("contract_pool_tickers", tickers, ""))
            except Exception as exc:
                self._enqueue_event(
                    (
                        "contract_pool_tickers",
                        {},
                        str(exc) or exc.__class__.__name__,
                    )
                )

        threading.Thread(
            target=load,
            name="contract-pool-tickers-loader",
            daemon=True,
        ).start()

    def _apply_contract_pool_tickers(
        self, tickers: object, error: str
    ) -> None:
        self._contract_pool_refresh_inflight = False
        if error or not isinstance(tickers, dict):
            return
        self._futures_tickers.update(
            {
                str(symbol).strip().upper(): dict(item)
                for symbol, item in tickers.items()
                if str(symbol).strip() and isinstance(item, dict)
            }
        )
        self._sync_contract_pool_table()

    def _show_gainers_context_menu(self, position: QPoint) -> None:
        self._show_rankings_context_menu(self.futures_gainers_tree, position)

    def _show_losers_context_menu(self, position: QPoint) -> None:
        self._show_rankings_context_menu(self.futures_losers_tree, position)

    def _show_rankings_context_menu(
        self, table: KeyedTable, position: QPoint
    ) -> None:
        index = table.indexAt(position)
        if not index.isValid():
            return
        clicked_symbol = table.get_children()[index.row()]
        if clicked_symbol not in table.selection():
            table.clearSelection()
            table.selectRow(index.row())
        menu = QMenu(table)
        add_action = menu.addAction("添加")
        add_action.triggered.connect(
            lambda _checked=False, selected_table=table:
            self._add_selected_rankings_to_pool(selected_table)
        )
        menu.exec(table.viewport().mapToGlobal(position))

    def _show_contract_pool_context_menu(self, position: QPoint) -> None:
        index = self.contract_pool_tree.indexAt(position)
        if not index.isValid():
            return
        clicked_symbol = self.contract_pool_tree.get_children()[index.row()]
        if clicked_symbol not in self.contract_pool_tree.selection():
            self.contract_pool_tree.clearSelection()
            self.contract_pool_tree.selectRow(index.row())
        menu = QMenu(self.contract_pool_tree)
        remove_action = menu.addAction("移除")
        remove_action.triggered.connect(
            lambda _checked=False: self._remove_selected_pool_contracts()
        )
        menu.exec(self.contract_pool_tree.viewport().mapToGlobal(position))

    def _refresh_futures_rankings(self) -> None:
        if self._closed or self._futures_rankings_inflight:
            return
        self._futures_rankings_inflight = True
        self.contract_pool_refresh_button.setEnabled(False)
        self.contract_pool_status_var.set("正在获取 Binance 合约行情……")

        def load() -> None:
            try:
                payload = self.backend_client.futures_rankings(limit=20)
                self._enqueue_event(("futures_rankings", payload, ""))
            except Exception as exc:
                self._enqueue_event(
                    (
                        "futures_rankings",
                        {},
                        str(exc) or exc.__class__.__name__,
                    )
                )

        threading.Thread(
            target=load,
            name="futures-rankings-loader",
            daemon=True,
        ).start()

    @staticmethod
    def _ranking_number(value: object, *, volume: bool = False) -> str:
        try:
            number = Decimal(str(value))
        except (ArithmeticError, ValueError):
            return "-"
        if not number.is_finite():
            return "-"
        if volume:
            return f"{number:,.0f}"
        places = 4 if abs(number) >= 1 else 8
        return f"{number:.{places}f}".rstrip("0").rstrip(".")

    def _apply_futures_rankings(
        self, payload: dict[str, object], error: str
    ) -> None:
        self._futures_rankings_inflight = False
        self.contract_pool_refresh_button.setEnabled(True)
        if error:
            self.contract_pool_status_var.set(f"涨跌榜刷新失败：{error}")
            return

        gainers = payload.get("gainers", [])
        losers = payload.get("losers", [])
        tickers = payload.get("tickers", {})
        if not isinstance(gainers, list) or not isinstance(losers, list):
            self.contract_pool_status_var.set("涨跌榜刷新失败：后端返回格式不正确")
            return
        if isinstance(tickers, dict):
            self._futures_tickers = {
                str(symbol).strip().upper(): dict(item)
                for symbol, item in tickers.items()
                if str(symbol).strip() and isinstance(item, dict)
            }
        self.futures_gainers_tree.clear_rows()
        self.futures_losers_tree.clear_rows()
        for table, rows, tag in (
            (self.futures_gainers_tree, gainers, "win"),
            (self.futures_losers_tree, losers, "loss"),
        ):
            for item in rows:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip().upper()
                if not symbol or table.exists(symbol):
                    continue
                change = self._ranking_number(item.get("price_change_percent"))
                if change != "-" and not change.startswith("-"):
                    change = "+" + change
                table.insert(
                    "",
                    None,
                    iid=symbol,
                    text=symbol,
                    values=(
                        f"{change}%",
                        self._ranking_number(item.get("last_price")),
                        self._ranking_number(
                            item.get("quote_volume"), volume=True
                        ),
                    ),
                    tags=(tag,),
                )
        timestamp = payload.get("updated_at")
        try:
            refreshed_at = datetime.fromtimestamp(
                int(timestamp) / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            refreshed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._futures_rankings_loaded = True
        if self.notebook.currentWidget() is self.contract_pool_page:
            self.contract_pool_timer.start(CONTRACT_POOL_REFRESH_MS)
        self._sync_contract_pool_table()
        self.contract_pool_status_var.set(
            f"更新于 {refreshed_at}；涨幅榜 {self.futures_gainers_tree.rowCount()} 个，"
            f"跌幅榜 {self.futures_losers_tree.rowCount()} 个。涨跌幅为滚动 24 小时数据，"
            "每 30 分钟自动刷新。"
        )

    def _sync_contract_pool_table(self) -> None:
        self.contract_pool_tree.clear_rows()
        for symbol in self.config.contract_pool:
            ticker = self._futures_tickers.get(symbol, {})
            change = self._ranking_number(ticker.get("price_change_percent"))
            if change != "-" and not change.startswith("-"):
                change = "+" + change
            change_text = "-" if change == "-" else f"{change}%"
            self.contract_pool_tree.insert(
                "", None, iid=symbol, text=symbol, values=(change_text,)
            )
            if change != "-":
                self.contract_pool_tree.set_cell_foreground(
                    symbol,
                    1,
                    COLORS["negative"]
                    if change.startswith("-")
                    else COLORS["positive"],
                )

    def _add_selected_gainers_to_pool(self) -> None:
        self._add_selected_rankings_to_pool(self.futures_gainers_tree)

    def _add_selected_rankings_to_pool(self, table: KeyedTable) -> None:
        selected = list(table.selection())
        if not selected:
            show_info("请选择合约", "请先在涨跌榜中选择至少一个合约。")
            return
        current = list(self.config.contract_pool)
        existing = set(current)
        added = [symbol for symbol in selected if symbol not in existing]
        if not added:
            show_info("无需添加", "所选合约已经在合约池中。")
            return
        if len(current) + len(added) > MAX_CONTRACT_POOL_SYMBOLS:
            show_error(
                "添加合约失败",
                f"合约池数量不能超过 {MAX_CONTRACT_POOL_SYMBOLS} 个。",
            )
            return
        try:
            updated = replace(self.config, contract_pool=[*current, *added])
            updated.validate()
            self.store.save(updated)
            self.config = updated
            self._sync_contract_pool_table()
            self.contract_pool_status_var.set(
                f"已添加 {', '.join(added)}；当前合约池共 {len(updated.contract_pool)} 个。"
            )
        except (BackendClientError, OSError, TypeError, ValueError) as exc:
            show_error("添加合约失败", str(exc))

    def _remove_selected_pool_contracts(self) -> None:
        selected = list(self.contract_pool_tree.selection())
        if not selected:
            show_info("请选择合约", "请先在合约池中选择至少一个合约。")
            return
        try:
            removed = set(selected)
            updated = replace(
                self.config,
                contract_pool=[
                    symbol
                    for symbol in self.config.contract_pool
                    if symbol not in removed
                ],
            )
            self.store.save(updated)
            self.config = updated
            self._sync_contract_pool_table()
            self.contract_pool_status_var.set(
                f"已移除 {', '.join(selected)}；当前合约池共 {len(updated.contract_pool)} 个。"
            )
        except (BackendClientError, OSError, TypeError, ValueError) as exc:
            show_error("移除合约失败", str(exc))

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

        ai_settings = QGroupBox("大模型开仓决策")
        ai_layout = QVBoxLayout(ai_settings)
        ai_layout.setSpacing(12)

        ai_header = QHBoxLayout()
        self.ai_enabled_checkbox = QCheckBox(
            "启用大模型决策（今日方向 + 候选开仓时机）"
        )
        self.ai_enabled_checkbox.setChecked(
            self.config.ai_provider != "DISABLED"
        )
        ai_header.addWidget(self.ai_enabled_checkbox)
        ai_header.addStretch()
        ai_header.addWidget(QLabel("模型模式"))
        self.ai_provider_combo = self._combo(
            self.ai_provider_var,
            ["CHATGPT", "DEEPSEEK", "QWEN", "DUAL"],
        )
        self.ai_provider_combo.setMinimumWidth(180)
        ai_header.addWidget(self.ai_provider_combo)
        ai_layout.addLayout(ai_header)

        provider_layout = QHBoxLayout()
        provider_layout.setSpacing(12)

        self.openai_settings_group = QGroupBox("OpenAI")
        openai_grid = QGridLayout(self.openai_settings_group)
        openai_grid.setColumnStretch(1, 1)
        self._grid_field(
            openai_grid,
            0,
            0,
            "API Key",
            self._line(self.openai_api_key_var, secret=True),
        )
        self._grid_field(
            openai_grid,
            1,
            0,
            "模型",
            self._line(self.openai_model_var),
        )
        self._grid_field(
            openai_grid,
            2,
            0,
            "推理设置",
            self._openai_reasoning_control(),
        )
        openai_note = QLabel(
            "GPT-5/o 系列可指定 reasoning.effort；关闭时不发送推理参数。"
        )
        openai_note.setWordWrap(True)
        openai_note.setStyleSheet(f"color: {COLORS['muted']};")
        openai_grid.addWidget(openai_note, 3, 0, 1, 2)
        provider_layout.addWidget(self.openai_settings_group, 1)

        self.deepseek_settings_group = QGroupBox("DeepSeek")
        deepseek_grid = QGridLayout(self.deepseek_settings_group)
        deepseek_grid.setColumnStretch(1, 1)
        self._grid_field(
            deepseek_grid,
            0,
            0,
            "API Key",
            self._line(self.deepseek_api_key_var, secret=True),
        )
        self._grid_field(
            deepseek_grid,
            1,
            0,
            "模型",
            self._line(self.deepseek_model_var),
        )
        self._grid_field(
            deepseek_grid,
            2,
            0,
            "推理设置",
            self._deepseek_reasoning_control(),
        )
        deepseek_note = QLabel(
            "V4 模型支持思考开关；强度 low / high / max。"
        )
        deepseek_note.setWordWrap(True)
        deepseek_note.setStyleSheet(f"color: {COLORS['muted']};")
        deepseek_grid.addWidget(deepseek_note, 3, 0, 1, 2)
        provider_layout.addWidget(self.deepseek_settings_group, 1)

        self.qwen_settings_group = QGroupBox("Qwen · 阿里云百炼")
        qwen_grid = QGridLayout(self.qwen_settings_group)
        qwen_grid.setColumnStretch(1, 1)
        self._grid_field(
            qwen_grid,
            0,
            0,
            "API Key",
            self._line(self.qwen_api_key_var, secret=True),
        )
        self._grid_field(
            qwen_grid,
            1,
            0,
            "模型",
            self._line(self.qwen_model_var),
        )
        self._grid_field(
            qwen_grid,
            2,
            0,
            "Chat 接口",
            self._line(self.qwen_chat_url_var),
        )
        self._grid_field(
            qwen_grid,
            3,
            0,
            "推理设置",
            self._qwen_reasoning_control(),
        )
        qwen_note = QLabel(
            "Qwen3+ 支持思考模式；具体强度档位随模型变化。"
        )
        qwen_note.setWordWrap(True)
        qwen_note.setStyleSheet(f"color: {COLORS['muted']};")
        qwen_grid.addWidget(qwen_note, 4, 0, 1, 2)
        provider_layout.addWidget(self.qwen_settings_group, 1)
        ai_layout.addLayout(provider_layout)

        self.ai_common_group = QGroupBox("通用决策参数")
        ai_grid = QGridLayout(self.ai_common_group)
        ai_grid.setHorizontalSpacing(12)
        ai_grid.setVerticalSpacing(10)
        ai_grid.setColumnStretch(1, 1)
        ai_grid.setColumnStretch(3, 1)
        self._grid_field(
            ai_grid,
            0,
            0,
            "最低置信度",
            self._line(self.ai_min_confidence_var),
        )
        self._grid_field(
            ai_grid,
            0,
            2,
            "决策超时(秒，最高600)",
            self._line(self.ai_timeout_var),
        )
        ai_history_line = self._line(self.ai_history_days_var)
        ai_history_line.setEnabled(False)
        self._grid_field(
            ai_grid,
            1,
            0,
            "方向日线(固定30根)",
            ai_history_line,
        )
        news_window = QWidget()
        news_window_layout = QHBoxLayout(news_window)
        news_window_layout.setContentsMargins(0, 0, 0, 0)
        news_window_layout.addWidget(self._line(self.ai_news_days_var))
        news_window_layout.addWidget(QLabel("/"))
        news_window_layout.addWidget(self._line(self.ai_news_limit_var))
        self._grid_field(
            ai_grid,
            1,
            2,
            "新闻天数/条数",
            news_window,
        )
        self._grid_field(
            ai_grid,
            2,
            0,
            "时机K线数量",
            self._line(self.ai_entry_timing_bars_var),
        )
        ai_layout.addWidget(self.ai_common_group)

        ai_note = QLabel(
            "开关关闭时完全使用表格中的手动方向，不调用大模型。"
            "开关开启时，模型先生成今日 LONG/SHORT/FLAT，"
            "方向判断使用最近30根日线OHLC；再使用今日日线和配置数量的"
            "五分钟K线OHLC判断每个候选信号的 ENTER/WAIT。"
            "失败、低置信度或双模型分歧时不开仓。"
            "Qwen 使用阿里云百炼 OpenAI 兼容 Chat 接口；新工作区可填写专属接口地址。"
            "大模型配置和 API Key 会保存到后端本地配置文件；"
            "OpenAI Key 也可用于交易经验上传。"
        )
        ai_note.setWordWrap(True)
        ai_note.setStyleSheet(f"color: {COLORS['muted']};")
        ai_layout.addWidget(ai_note)

        self.ai_provider_combo.currentTextChanged.connect(
            self._update_ai_provider_layout
        )
        self.ai_enabled_checkbox.toggled.connect(
            self._update_ai_controls_enabled
        )
        self._update_ai_provider_layout(self.ai_provider_var.get())
        self._update_ai_controls_enabled(
            self.ai_enabled_checkbox.isChecked()
        )
        content_layout.addWidget(ai_settings)
        content_layout.addStretch()

    def _build_trade_history_page(self) -> None:
        layout = QVBoxLayout(self.trade_history_page)
        layout.setContentsMargins(14, 8, 14, 14)
        layout.setSpacing(8)

        filters = QGroupBox("成交记录查询")
        filter_layout = QHBoxLayout(filters)
        filter_layout.addWidget(QLabel("标的"))
        symbol = self._line(self.trade_history_symbol_var)
        symbol.setPlaceholderText("留空查询全部")
        symbol.setMaximumWidth(180)
        symbol.returnPressed.connect(self._refresh_trade_history)
        filter_layout.addWidget(symbol)
        filter_layout.addWidget(QLabel("类型"))
        filter_layout.addWidget(
            self._combo(self.trade_history_action_var, ["全部", "开仓", "平仓"])
        )
        filter_layout.addWidget(QLabel("模式"))
        filter_layout.addWidget(
            self._combo(self.trade_history_mode_var, ["全部", "模拟", "实盘"])
        )
        filter_layout.addWidget(QLabel("最多条数"))
        limit = self._line(self.trade_history_limit_var)
        limit.setMaximumWidth(90)
        limit.returnPressed.connect(self._refresh_trade_history)
        filter_layout.addWidget(limit)
        filter_layout.addStretch()
        self.trade_history_refresh_button = self._button(
            "查询记录",
            self._refresh_trade_history,
            primary=True,
        )
        filter_layout.addWidget(self.trade_history_refresh_button)
        layout.addWidget(filters)

        layout.addWidget(
            self._bound_label(
                self.trade_history_status_var,
                muted=True,
                wrap=True,
            )
        )
        headers = [
            "成交时间",
            "标的",
            "类型",
            "开仓方向",
            "价格",
            "数量",
            "金额",
            "手续费",
            "收益",
            "交易模式",
        ]
        widths = [155, 90, 70, 85, 90, 100, 100, 90, 100, 70]
        self.trade_history_tree = KeyedTable(headers, widths, multi_select=False)
        layout.addWidget(self.trade_history_tree, 1)

    def _refresh_trade_history(self) -> None:
        if self._trade_history_inflight:
            return
        try:
            limit = int(self.trade_history_limit_var.get().strip())
            if not 1 <= limit <= 1000:
                raise ValueError("最多条数必须在 1 到 1000 之间")
        except ValueError as exc:
            show_error("查询条件错误", str(exc))
            return
        action = {
            "全部": "ALL",
            "开仓": "OPEN",
            "平仓": "CLOSE",
        }[self.trade_history_action_var.get()]
        mode = {
            "全部": "ALL",
            "模拟": "PAPER",
            "实盘": "REAL",
        }[self.trade_history_mode_var.get()]
        symbol = self.trade_history_symbol_var.get().strip().upper()
        self._trade_history_inflight = True
        self.trade_history_refresh_button.setEnabled(False)
        self.trade_history_status_var.set("正在查询持久化成交记录……")

        def query() -> None:
            try:
                items = self.controller.trade_history(
                    symbol=symbol,
                    action=action,
                    mode=mode,
                    limit=limit,
                )
                self._enqueue_event(("trade_history", items))
            except Exception as exc:
                self._enqueue_event(("trade_history_error", str(exc)))

        threading.Thread(
            target=query,
            name="trade-history-query",
            daemon=True,
        ).start()

    def _apply_trade_history(self, items: list[TradeHistoryItem]) -> None:
        self._trade_history_inflight = False
        self.trade_history_refresh_button.setEnabled(True)
        self.trade_history_tree.clear_rows()
        action_text = {"OPEN": "开仓", "CLOSE": "平仓"}
        direction_text = {"LONG": "多头", "SHORT": "空头"}
        total_profit = Decimal("0")
        close_count = 0
        for index, item in enumerate(items):
            if item.action == "CLOSE":
                close_count += 1
                total_profit += item.profit
            tag = (
                "win"
                if item.profit > 0
                else "loss"
                if item.profit < 0
                else ""
            )
            executed_at = datetime.fromtimestamp(
                item.executed_at / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")
            self.trade_history_tree.insert(
                "",
                None,
                iid=f"trade-history-{item.executed_at}-{index}",
                text=executed_at,
                values=(
                    item.symbol,
                    action_text.get(item.action, item.action),
                    direction_text.get(
                        item.opening_direction,
                        item.opening_direction,
                    ),
                    f"{item.price:.2f}",
                    self._format_decimal(item.quantity),
                    f"{item.amount:.2f}",
                    f"{item.fee:.2f}",
                    f"{item.profit:.2f}",
                    "模拟" if item.paper else "实盘",
                ),
                tags=(tag,) if tag else (),
            )
        self.trade_history_status_var.set(
            f"共查询到 {len(items)} 条成交记录；"
            f"其中平仓 {close_count} 条，平仓收益合计 {total_profit:.2f}"
        )

    def _apply_trade_history_error(self, message: str) -> None:
        self._trade_history_inflight = False
        self.trade_history_refresh_button.setEnabled(True)
        self.trade_history_status_var.set(f"查询失败：{message}")
        show_error("交易记录查询失败", message)

    def _build_ai_decision_page(self) -> None:
        layout = QVBoxLayout(self.ai_decision_page)
        layout.setContentsMargins(14, 8, 14, 14)
        layout.setSpacing(8)

        filters = QGroupBox("AI 决策记录筛选")
        filter_layout = QHBoxLayout(filters)
        filter_layout.addWidget(QLabel("标的"))
        symbol = self._line(self.ai_decision_symbol_var)
        symbol.setPlaceholderText("全部，或输入 SOXLUSDT")
        symbol.setMaximumWidth(190)
        symbol.returnPressed.connect(self._refresh_ai_decisions)
        filter_layout.addWidget(symbol)
        filter_layout.addWidget(QLabel("阶段"))
        filter_layout.addWidget(
            self._combo(
                self.ai_decision_stage_var,
                ["全部", "今日方向", "开仓时机"],
            )
        )
        filter_layout.addWidget(QLabel("最多条数"))
        limit = self._line(self.ai_decision_limit_var)
        limit.setMaximumWidth(90)
        limit.returnPressed.connect(self._refresh_ai_decisions)
        filter_layout.addWidget(limit)
        filter_layout.addStretch()
        self.ai_decision_refresh_button = self._button(
            "查询决策",
            self._refresh_ai_decisions,
            primary=True,
        )
        filter_layout.addWidget(self.ai_decision_refresh_button)
        layout.addWidget(filters)
        layout.addWidget(
            self._bound_label(
                self.ai_decision_status_var,
                muted=True,
                wrap=True,
            )
        )

        headers = [
            "决策时间",
            "标的",
            "阶段",
            "供应商",
            "模型",
            "结果",
            "置信度",
            "安全兜底",
            "总耗时",
            "响应时间",
            "结论摘要",
        ]
        widths = [155, 90, 90, 90, 155, 75, 75, 85, 75, 85, 360]
        self.ai_decision_tree = KeyedTable(
            headers,
            widths,
            multi_select=False,
        )
        self.ai_decision_tree.itemSelectionChanged.connect(
            self._show_ai_decision_detail
        )

        detail_tabs = QTabWidget()
        self.ai_decision_result_detail = QTextEdit()
        self.ai_decision_input_detail = QTextEdit()
        self.ai_decision_output_detail = QTextEdit()
        for widget in (
            self.ai_decision_result_detail,
            self.ai_decision_input_detail,
            self.ai_decision_output_detail,
        ):
            widget.setReadOnly(True)
            widget.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.ai_decision_result_detail.setPlaceholderText(
            "选择一条记录查看最终决策"
        )
        self.ai_decision_input_detail.setPlaceholderText(
            "选择一条记录查看完整 AI 输入"
        )
        self.ai_decision_output_detail.setPlaceholderText(
            "选择一条记录查看原始 API 输出"
        )
        detail_tabs.addTab(self.ai_decision_result_detail, "最终决策")
        detail_tabs.addTab(self.ai_decision_input_detail, "完整输入")
        detail_tabs.addTab(self.ai_decision_output_detail, "原始输出")

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.ai_decision_tree)
        splitter.addWidget(detail_tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([410, 260])
        layout.addWidget(splitter, 1)

    def _refresh_ai_decisions(self) -> None:
        if self._ai_decision_inflight:
            return
        try:
            limit = int(self.ai_decision_limit_var.get().strip())
            if not 1 <= limit <= 500:
                raise ValueError("最多条数必须在 1 到 500 之间")
        except ValueError as exc:
            show_error("查询条件错误", str(exc))
            return
        stage = {
            "全部": "ALL",
            "今日方向": "OPENING_DIRECTION",
            "开仓时机": "ENTRY_TIMING",
        }[self.ai_decision_stage_var.get()]
        symbol = self.ai_decision_symbol_var.get().strip().upper()
        self._ai_decision_inflight = True
        self.ai_decision_refresh_button.setEnabled(False)
        self.ai_decision_status_var.set(
            "正在查询持久化 AI 输入、输出与决策结果……"
        )

        def query() -> None:
            try:
                items = self.controller.ai_decision_history(
                    symbol=symbol,
                    stage=stage,
                    limit=limit,
                )
                self._enqueue_event(("ai_decisions", items))
            except Exception as exc:
                self._enqueue_event(("ai_decisions_error", str(exc)))

        threading.Thread(
            target=query,
            name="ai-decision-history-query",
            daemon=True,
        ).start()

    def _apply_ai_decisions(
        self, items: list[AiDecisionHistoryItem]
    ) -> None:
        self._ai_decision_inflight = False
        self.ai_decision_refresh_button.setEnabled(True)
        self.ai_decision_tree.clear_rows()
        self._ai_decisions = {item.record_id: item for item in items}
        stage_text = {
            "OPENING_DIRECTION": "今日方向",
            "ENTRY_TIMING": "开仓时机",
        }
        outcome_text = {
            "LONG": "LONG",
            "SHORT": "SHORT",
            "FLAT": "FLAT",
            "ENTER": "入场",
            "WAIT": "等待",
        }
        fallback_count = 0
        for item in items:
            if item.fallback:
                fallback_count += 1
            decided_at = datetime.fromtimestamp(
                item.decided_at / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")
            tag = "error" if item.fallback else "signal"
            self.ai_decision_tree.insert(
                "",
                None,
                iid=item.record_id,
                text=decided_at,
                values=(
                    item.symbol,
                    stage_text.get(item.stage, item.stage),
                    item.provider,
                    item.model or "-",
                    outcome_text.get(item.outcome, item.outcome),
                    f"{item.confidence:.0%}",
                    "是" if item.fallback else "否",
                    f"{item.elapsed_ms} ms",
                    f"{item.response_ms} ms",
                    item.summary,
                ),
                tags=(tag,),
            )
        self.ai_decision_status_var.set(
            f"共查询到 {len(items)} 条 AI 决策；"
            f"其中安全兜底 {fallback_count} 条"
        )
        if items:
            self.ai_decision_tree.selectRow(0)
            self._show_ai_decision_detail()
        else:
            self.ai_decision_result_detail.clear()
            self.ai_decision_input_detail.clear()
            self.ai_decision_output_detail.clear()

    def _show_ai_decision_detail(self) -> None:
        selected = self.ai_decision_tree.selection()
        if not selected:
            return
        item = self._ai_decisions.get(selected[0])
        if item is None:
            return
        factors = "\n".join(f"- {value}" for value in item.factors) or "- 无"
        risks = "\n".join(f"- {value}" for value in item.risks) or "- 无"
        self.ai_decision_result_detail.setPlainText(
            f"结果：{item.outcome}\n"
            f"置信度：{item.confidence:.2%}\n"
            f"供应商/模型：{item.provider}/{item.model or '-'}\n"
            f"安全兜底：{'是' if item.fallback else '否'}\n"
            f"总决策耗时：{item.elapsed_ms} ms\n"
            f"模型响应时间：{item.response_ms} ms\n\n"
            f"结论\n{item.summary}\n\n"
            f"主要依据\n{factors}\n\n"
            f"主要风险\n{risks}"
        )
        self.ai_decision_input_detail.setPlainText(
            self._pretty_json(item.input_json)
        )
        self.ai_decision_output_detail.setPlainText(
            self._pretty_json(item.output_json)
        )

    @staticmethod
    def _pretty_json(raw: str) -> str:
        try:
            return json.dumps(
                json.loads(raw),
                ensure_ascii=False,
                indent=2,
            )
        except (TypeError, json.JSONDecodeError):
            return raw

    def _apply_ai_decisions_error(self, message: str) -> None:
        self._ai_decision_inflight = False
        self.ai_decision_refresh_button.setEnabled(True)
        self.ai_decision_status_var.set(f"查询失败：{message}")
        show_error("AI 决策记录查询失败", message)

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

    def _reasoning_control(
        self,
        *,
        provider: str,
        enabled: bool,
        effort_var: TextValue,
        effort_values: list[str],
        tooltip: str,
    ) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        checkbox = QCheckBox("启用")
        checkbox.setChecked(enabled)
        checkbox.setToolTip(tooltip)
        effort = self._combo(effort_var, effort_values)
        effort.setMinimumWidth(110)
        effort.setToolTip(tooltip)
        effort.setEnabled(enabled)
        checkbox.toggled.connect(effort.setEnabled)
        layout.addWidget(checkbox)
        layout.addWidget(QLabel("强度"))
        layout.addWidget(effort)
        layout.addStretch()
        setattr(self, f"{provider}_reasoning_checkbox", checkbox)
        setattr(self, f"{provider}_reasoning_effort_combo", effort)
        return container

    def _openai_reasoning_control(self) -> QWidget:
        return self._reasoning_control(
            provider="openai",
            enabled=self.config.openai_reasoning_enabled,
            effort_var=self.openai_reasoning_effort_var,
            effort_values=["low", "medium", "high", "xhigh", "max"],
            tooltip=(
                "启用后向 OpenAI Responses API 发送 reasoning.effort；"
                "关闭时不发送该参数，以兼容非推理模型。"
            ),
        )

    def _deepseek_reasoning_control(self) -> QWidget:
        control = self._reasoning_control(
            provider="deepseek",
            enabled=self.config.deepseek_thinking_enabled,
            effort_var=self.deepseek_reasoning_effort_var,
            effort_values=["low", "medium", "high", "max"],
            tooltip=(
                "启用后发送 thinking=enabled 和 reasoning_effort；"
                "DeepSeek V4 会把 medium 映射为 high。"
            ),
        )
        self.deepseek_thinking_checkbox = self.deepseek_reasoning_checkbox
        return control

    def _qwen_reasoning_control(self) -> QWidget:
        return self._reasoning_control(
            provider="qwen",
            enabled=self.config.qwen_thinking_enabled,
            effort_var=self.qwen_reasoning_effort_var,
            effort_values=["low", "medium", "high", "xhigh", "max"],
            tooltip=(
                "启用后向百炼 Chat 接口发送 enable_thinking=true 和 "
                "reasoning_effort；支持档位由具体 Qwen 模型决定。"
            ),
        )

    def _update_ai_provider_layout(self, mode: str = "") -> None:
        selected = (mode or self.ai_provider_var.get()).strip().upper()
        self.openai_settings_group.setVisible(
            selected in {"CHATGPT", "DUAL"}
        )
        self.deepseek_settings_group.setVisible(
            selected in {"DEEPSEEK", "DUAL"}
        )
        self.qwen_settings_group.setVisible(selected == "QWEN")

    def _update_ai_controls_enabled(self, enabled: bool) -> None:
        self.openai_settings_group.setEnabled(enabled)
        self.deepseek_settings_group.setEnabled(enabled)
        self.qwen_settings_group.setEnabled(enabled)
        self.ai_common_group.setEnabled(enabled)

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
            f"含K线 {summary.with_kline}｜交易净盈亏 {summary.net_pnl:.2f}"
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
        if not api_key or api_key == SECRET_SENTINEL:
            show_error(
                "缺少凭据",
                "请先在“运行配置”页重新填写真实 OpenAI API Key。",
            )
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
            contract_pool=list(self.config.contract_pool),
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

    def _build_backtest_page(self) -> None:
        layout = QVBoxLayout(self.backtest_page)
        layout.setContentsMargins(14, 8, 14, 14)
        layout.setSpacing(8)

        controls = QGroupBox("历史数据与策略回测")
        controls_layout = QHBoxLayout(controls)
        controls_layout.addWidget(QLabel("标的"))
        symbol = self._line(self.backtest_symbol_var)
        symbol.setPlaceholderText("例如 BTCUSDT")
        symbol.setMaximumWidth(180)
        controls_layout.addWidget(symbol)
        controls_layout.addWidget(QLabel("策略"))
        strategy = self._combo(
            self.backtest_strategy_var, ["five_minute_breakout"]
        )
        strategy.setMaximumWidth(230)
        controls_layout.addWidget(strategy)
        self.backtest_download_button = self._button(
            "下载 180 天 K 线", self._start_backtest_download, primary=True
        )
        self.backtest_run_button = self._button(
            "执行回测", self._start_backtest_run, primary=True
        )
        self.backtest_export_button = self._button(
            "导出 K 线", self._export_historical_bars
        )
        self.backtest_import_button = self._button(
            "导入 K 线", self._import_historical_bars
        )
        self.backtest_refresh_button = self._button(
            "刷新", self._refresh_backtest_data
        )
        controls_layout.addWidget(self.backtest_download_button)
        controls_layout.addWidget(self.backtest_run_button)
        controls_layout.addWidget(self.backtest_export_button)
        controls_layout.addWidget(self.backtest_import_button)
        controls_layout.addWidget(self.backtest_refresh_button)
        controls_layout.addStretch()
        layout.addWidget(controls)

        note = QLabel(
            "使用当前运行配置中的行情源、MA、开仓金额、每日交易次数及止盈止损参数。"
            "最多回看 180 天，标的历史不足时以行情源实际返回数量为准；"
            "收益率 = 总盈亏 ÷ 单笔开仓金额，最大回撤按单笔开仓金额作为初始资金计算；"
            "金额和百分比统一显示两位小数。分页下载目前支持 Binance Futures，"
            "数据和回测结果均保存在后端 SQLite。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['muted']};")
        layout.addWidget(note)

        status = QLabel()
        self.backtest_status_var.bind_label(status)
        status.setWordWrap(True)
        status.setStyleSheet(f"color: {COLORS['muted']};")
        layout.addWidget(status)

        downloads_box = QGroupBox("历史 K 线下载记录")
        downloads_layout = QVBoxLayout(downloads_box)
        self.backtest_download_tree = KeyedTable(
            [
                "创建时间", "标的", "行情源", "状态", "进度",
                "日线", "5分钟", "1分钟", "说明",
            ],
            [150, 100, 125, 80, 70, 65, 75, 80, 260],
            multi_select=False,
        )
        self.backtest_download_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.backtest_download_tree.customContextMenuRequested.connect(
            self._show_backtest_download_context_menu
        )
        downloads_layout.addWidget(self.backtest_download_tree)

        runs_box = QGroupBox("持久化回测结果")
        runs_layout = QVBoxLayout(runs_box)
        self.backtest_run_tree = KeyedTable(
            [
                "完成时间", "标的", "策略", "状态", "交易数", "胜/负",
                "总盈亏", "收益率", "最大回撤", "回测明细", "说明",
            ],
            [150, 100, 170, 80, 65, 70, 105, 85, 90, 90, 220],
            multi_select=False,
        )
        runs_layout.addWidget(self.backtest_run_tree)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(downloads_box)
        splitter.addWidget(runs_box)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([310, 310])
        layout.addWidget(splitter, 1)

    def _backtest_symbol(self) -> str:
        symbol = self.backtest_symbol_var.get().strip().upper()
        if not symbol:
            raise ValueError("请输入回测标的")
        if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,19}", symbol) is None:
            raise ValueError("回测标的格式不正确")
        return symbol

    def _set_backtest_actions_enabled(self, enabled: bool) -> None:
        self.backtest_download_button.setEnabled(enabled)
        self.backtest_run_button.setEnabled(enabled)
        self.backtest_export_button.setEnabled(enabled)
        self.backtest_import_button.setEnabled(enabled)

    def _start_backtest_download(self) -> None:
        if self._backtest_action_inflight:
            return
        try:
            symbol = self._backtest_symbol()
        except ValueError as exc:
            show_error("无法下载", str(exc))
            return
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_status_var.set(f"正在提交 {symbol} 的 180 天历史 K 线下载任务…")

        def submit() -> None:
            try:
                job_id = self.backend_client.start_historical_download(symbol)
                self._enqueue_event(("backtest_action", "download", job_id, ""))
            except Exception as exc:
                self._enqueue_event(("backtest_action", "download", "", str(exc)))

        threading.Thread(
            target=submit, name="backtest-download-submit", daemon=True
        ).start()

    def _start_backtest_run(self) -> None:
        if self._backtest_action_inflight:
            return
        try:
            symbol = self._backtest_symbol()
            strategy = self.backtest_strategy_var.get().strip()
            if not strategy:
                raise ValueError("请选择回测策略")
        except ValueError as exc:
            show_error("无法回测", str(exc))
            return
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_status_var.set(f"正在提交 {symbol} 的策略回测任务…")

        def submit() -> None:
            try:
                run_id = self.backend_client.start_backtest(symbol, strategy)
                self._enqueue_event(("backtest_action", "run", run_id, ""))
            except Exception as exc:
                self._enqueue_event(("backtest_action", "run", "", str(exc)))

        threading.Thread(
            target=submit, name="backtest-run-submit", daemon=True
        ).start()

    def _export_historical_bars(self) -> None:
        if self._backtest_action_inflight:
            return
        try:
            symbol = self._backtest_symbol()
        except ValueError as exc:
            show_error("无法导出", str(exc))
            return
        self._begin_historical_export(symbol, "")

    def _show_backtest_download_context_menu(self, position: QPoint) -> None:
        index = self.backtest_download_tree.indexAt(position)
        if not index.isValid():
            return
        self.backtest_download_tree.selectRow(index.row())
        selected = self.backtest_download_tree.selection()
        if not selected:
            return
        item = self._backtest_downloads.get(selected[0])
        if item is None:
            return
        symbol = str(item.get("symbol", "")).strip().upper()
        provider = str(item.get("provider", "")).strip().lower()
        status = str(item.get("status", ""))
        menu = QMenu(self.backtest_download_tree)
        export_action = menu.addAction("导出该标的 K 线")
        delete_action = menu.addAction("删除该标的 K 线")
        export_action.setEnabled(
            sum(
                int(item.get(field, 0) or 0)
                for field in (
                    "daily_count",
                    "five_minute_count",
                    "one_minute_count",
                )
            )
            > 0
        )
        delete_action.setEnabled(status not in {"QUEUED", "RUNNING"})
        export_action.triggered.connect(
            lambda _checked=False: self._begin_historical_export(
                symbol, provider
            )
        )
        delete_action.triggered.connect(
            lambda _checked=False: self._delete_historical_bars(
                symbol, provider, item
            )
        )
        menu.exec(
            self.backtest_download_tree.viewport().mapToGlobal(position)
        )

    def _begin_historical_export(
        self, symbol: str, provider: str
    ) -> None:
        if self._backtest_action_inflight:
            return
        default_name = (
            f"{symbol}_{provider}_historical_klines.zip"
            if provider
            else f"{symbol}_historical_klines.zip"
        )
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "导出历史 K 线",
            default_name,
            "AutoQuant K线数据包 (*.zip)",
        )
        if not selected:
            return
        target = Path(selected)
        if target.suffix.lower() != ".zip":
            target = target.with_suffix(".zip")
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_status_var.set(f"正在导出 {symbol} 的历史 K 线…")

        def export() -> None:
            try:
                archive = self.backend_client.export_historical_bars(
                    symbol, provider
                )
                target.write_bytes(archive)
                self._enqueue_event(
                    ("backtest_archive", "export", str(target), {}, "")
                )
            except Exception as exc:
                self._enqueue_event(
                    ("backtest_archive", "export", str(target), {}, str(exc))
                )

        threading.Thread(
            target=export, name="backtest-bars-export", daemon=True
        ).start()

    def _delete_historical_bars(
        self,
        symbol: str,
        provider: str,
        item: dict[str, object],
    ) -> None:
        if self._backtest_action_inflight:
            return
        bar_count = sum(
            int(item.get(field, 0) or 0)
            for field in (
                "daily_count",
                "five_minute_count",
                "one_minute_count",
            )
        )
        if not ask_yes_no(
            "确认删除历史 K 线",
            f"将删除 {symbol}（{provider}）已持久化的约 {bar_count} 根 K 线，"
            "并清除该标的的下载/导入记录。\n\n"
            "已保存的回测结果不会删除。此操作不可撤销，确认继续吗？",
        ):
            return
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_status_var.set(f"正在删除 {symbol} 的历史 K 线…")

        def delete() -> None:
            try:
                result = self.backend_client.delete_historical_bars(
                    symbol, provider
                )
                self._enqueue_event(("backtest_delete", result, ""))
            except Exception as exc:
                self._enqueue_event(("backtest_delete", {}, str(exc)))

        threading.Thread(
            target=delete, name="backtest-bars-delete", daemon=True
        ).start()

    def _apply_backtest_delete(
        self, result: dict[str, object], error: str
    ) -> None:
        self._backtest_action_inflight = False
        self._set_backtest_actions_enabled(True)
        if error:
            self.backtest_status_var.set(f"历史 K 线删除失败：{error}")
            show_error("历史 K 线删除失败", error)
            return
        message = (
            f"已删除 {result.get('symbol', '')} 的 "
            f"{result.get('deleted_bars', 0)} 根历史 K 线和 "
            f"{result.get('deleted_downloads', 0)} 条下载/导入记录；"
            "回测结果已保留。"
        )
        self.backtest_status_var.set(message)
        show_info("删除完成", message)
        self._refresh_backtest_data()

    def _import_historical_bars(self) -> None:
        if self._backtest_action_inflight:
            return
        try:
            symbol = self._backtest_symbol()
        except ValueError as exc:
            show_error("无法导入", str(exc))
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "导入历史 K 线",
            "",
            "AutoQuant K线数据包 (*.zip)",
        )
        if not selected:
            return
        source = Path(selected)
        try:
            if source.stat().st_size > 128 * 1024 * 1024:
                raise ValueError("导入文件超过 128 MB 限制")
        except (OSError, ValueError) as exc:
            show_error("无法导入", str(exc))
            return
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_status_var.set(f"正在导入 {symbol} 的历史 K 线…")

        def import_bars() -> None:
            try:
                result = self.backend_client.import_historical_bars(
                    source.read_bytes(), expected_symbol=symbol
                )
                self._enqueue_event(
                    ("backtest_archive", "import", str(source), result, "")
                )
            except Exception as exc:
                self._enqueue_event(
                    ("backtest_archive", "import", str(source), {}, str(exc))
                )

        threading.Thread(
            target=import_bars, name="backtest-bars-import", daemon=True
        ).start()

    def _apply_backtest_archive(
        self,
        action: str,
        path: str,
        result: dict[str, object],
        error: str,
    ) -> None:
        self._backtest_action_inflight = False
        self._set_backtest_actions_enabled(True)
        if error:
            title = "历史 K 线导出失败" if action == "export" else "历史 K 线导入失败"
            self.backtest_status_var.set(f"{title}：{error}")
            show_error(title, error)
            return
        if action == "export":
            message = f"历史 K 线已导出到：\n{path}"
            self.backtest_status_var.set(message.replace("\n", " "))
            show_info("导出完成", message)
        else:
            counts = result.get("counts", {})
            if not isinstance(counts, dict):
                counts = {}
            message = (
                f"{result.get('symbol', '')} 导入完成："
                f"日线 {counts.get('1d', 0)} 根、5分钟 {counts.get('5m', 0)} 根、"
                f"1分钟 {counts.get('1m', 0)} 根。"
            )
            self.backtest_status_var.set(message)
            show_info("导入完成", message)
            self._refresh_backtest_data()

    def _apply_backtest_action(
        self, action: str, identifier: str, error: str
    ) -> None:
        self._backtest_action_inflight = False
        self._set_backtest_actions_enabled(True)
        if error:
            title = "历史数据下载失败" if action == "download" else "回测启动失败"
            self.backtest_status_var.set(f"{title}：{error}")
            show_error(title, error)
        else:
            label = "下载" if action == "download" else "回测"
            self.backtest_status_var.set(
                f"{label}任务已提交（{identifier[:8]}），后台执行中。"
            )
        self._refresh_backtest_data()

    def _refresh_backtest_data(self) -> None:
        if self._closed or self._backtest_refresh_inflight:
            return
        self._backtest_refresh_inflight = True
        self.backtest_refresh_button.setEnabled(False)

        def refresh() -> None:
            try:
                downloads = self.backend_client.historical_downloads()
                runs = self.backend_client.backtest_runs()
                self._enqueue_event(("backtest_data", downloads, runs, ""))
            except Exception as exc:
                self._enqueue_event(("backtest_data", [], [], str(exc)))

        threading.Thread(
            target=refresh, name="backtest-data-refresh", daemon=True
        ).start()

    @staticmethod
    def _backtest_datetime(timestamp: object) -> str:
        try:
            value = int(timestamp)
        except (TypeError, ValueError):
            return "—"
        if value <= 0:
            return "—"
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _backtest_metric(value: object, suffix: str = "") -> str:
        try:
            number = Decimal(str(value))
            if not number.is_finite():
                raise ValueError
        except (ArithmeticError, ValueError):
            return "—"
        return f"{number:.2f}{suffix}"

    def _load_backtest_trade_details(
        self, run_id: str, summary: dict[str, object]
    ) -> None:
        if self._backtest_detail_inflight:
            return
        self._backtest_detail_inflight = True
        self.backtest_status_var.set(
            f"正在读取 {summary.get('symbol', '')} 的回测明细…"
        )

        def load() -> None:
            try:
                items = self.backend_client.backtest_trade_details(run_id)
                self._enqueue_event(
                    ("backtest_details", summary, items, "")
                )
            except Exception as exc:
                self._enqueue_event(
                    ("backtest_details", summary, [], str(exc))
                )

        threading.Thread(
            target=load, name="backtest-trade-details", daemon=True
        ).start()

    def _apply_backtest_trade_details(
        self,
        summary: dict[str, object],
        items: list[dict[str, object]],
        error: str,
    ) -> None:
        self._backtest_detail_inflight = False
        if error:
            self.backtest_status_var.set(f"回测明细读取失败：{error}")
            show_error("回测明细读取失败", error)
            return
        self.backtest_status_var.set(
            f"已读取 {summary.get('symbol', '')} 的 {len(items)} 笔回测明细。"
        )
        self._show_backtest_trade_detail_dialog(summary, items)

    def _build_backtest_pnl_chart(
        self,
        items: list[dict[str, object]],
        currency: str,
    ) -> tuple[QChartView, QLabel]:
        cumulative_values: list[tuple[int, float, Decimal]] = []
        cumulative = Decimal("0")
        for index, item in enumerate(items, start=1):
            try:
                pnl = Decimal(str(item.get("pnl", "0")))
                if not pnl.is_finite():
                    pnl = Decimal("0")
            except ArithmeticError:
                pnl = Decimal("0")
            cumulative += pnl
            cumulative_values.append((index, float(cumulative), pnl))

        max_points = 2000
        step = max(1, (len(cumulative_values) + max_points - 1) // max_points)
        sampled_indices = list(range(0, len(cumulative_values), step))
        if cumulative_values and sampled_indices[-1] != len(cumulative_values) - 1:
            sampled_indices.append(len(cumulative_values) - 1)

        curve = QLineSeries()
        curve.setName("累计盈亏")
        curve_pen = QPen(QColor(COLORS["primary"]), 2.2)
        curve.setPen(curve_pen)
        curve.append(0, 0)
        wins = QScatterSeries()
        wins.setName("盈利交易")
        wins.setColor(QColor(COLORS["positive"]))
        wins.setBorderColor(QColor(COLORS["positive"]))
        wins.setMarkerSize(6)
        losses = QScatterSeries()
        losses.setName("亏损交易")
        losses.setColor(QColor(COLORS["negative"]))
        losses.setBorderColor(QColor(COLORS["negative"]))
        losses.setMarkerSize(6)
        for sampled_index in sampled_indices:
            trade_number, equity, pnl = cumulative_values[sampled_index]
            curve.append(trade_number, equity)
            if pnl > 0:
                wins.append(trade_number, equity)
            elif pnl < 0:
                losses.append(trade_number, equity)

        count = len(cumulative_values)
        zero = QLineSeries()
        zero.setName("盈亏零轴")
        zero.append(0, 0)
        zero.append(max(1, count), 0)
        zero.setPen(
            QPen(QColor("#98a2b3"), 1, Qt.PenStyle.DashLine)
        )
        guide = QLineSeries()
        guide.setName("当前交易")
        guide.setPen(
            QPen(QColor(COLORS["primary"]), 1, Qt.PenStyle.DashLine)
        )
        guide.setVisible(False)

        chart = QChart()
        chart.addSeries(curve)
        chart.addSeries(wins)
        chart.addSeries(losses)
        chart.addSeries(zero)
        chart.addSeries(guide)
        sample_note = (
            f"，抽样显示 {len(sampled_indices)}/{count} 个点"
            if count > max_points
            else ""
        )
        chart.setTitle(
            f"累计盈亏曲线（{currency}，共 {count} 笔{sample_note}）"
        )
        chart.setBackgroundVisible(False)
        chart.setPlotAreaBackgroundVisible(True)
        chart.setPlotAreaBackgroundBrush(QColor("#fbfcfe"))
        chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        for marker in chart.legend().markers(zero):
            marker.setVisible(False)
        for marker in chart.legend().markers(guide):
            marker.setVisible(False)

        axis_x = QValueAxis()
        axis_x.setTitleText("交易序号")
        axis_x.setRange(0, max(1, count))
        axis_x.setLabelFormat("%d")
        axis_x.setTickCount(min(11, max(2, count + 1)))
        all_equity = [0.0, *(value for _index, value, _pnl in cumulative_values)]
        minimum = min(all_equity)
        maximum = max(all_equity)
        span = maximum - minimum
        padding = max(span * 0.12, max(abs(minimum), abs(maximum), 1.0) * 0.05)
        axis_y = QValueAxis()
        axis_y.setTitleText(f"累计盈亏（{currency}）")
        axis_y.setRange(minimum - padding, maximum + padding)
        axis_y.setLabelFormat("%.2f")
        axis_y.setTickCount(6)
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        for series in (curve, wins, losses, zero, guide):
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)

        view = InteractiveChartView(chart)
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setMinimumHeight(250)
        view.setToolTip(
            "蓝线为逐笔累计盈亏；绿点表示盈利交易，红点表示亏损交易。"
            "将鼠标悬停或点击曲线上的点可查看对应交易。"
        )
        detail = QLabel(
            "将鼠标悬停或点击图表中的曲线、盈利点或亏损点查看对应交易明细。"
        )
        detail.setObjectName("backtestChartDetail")
        detail.setWordWrap(True)
        detail.setMinimumHeight(68)
        detail.setStyleSheet(
            f"""
            QLabel#backtestChartDetail {{
                color: {COLORS['text']};
                background: #f7faff;
                border: 1px solid #cfe0f7;
                border-radius: 7px;
                padding: 8px 11px;
            }}
            """
        )
        side_text = {"LONG": "多头", "SHORT": "空头"}
        exit_text = {
            "STOP_LOSS": "止损",
            "TAKE_PROFIT": "止盈",
            "END_OF_DATA": "数据结束",
        }

        def show_point(point: QPointF, *, clicked: bool) -> None:
            if not items:
                return
            trade_number = min(
                len(items), max(1, int(point.x() + 0.5))
            )
            item = items[trade_number - 1]
            pnl = Decimal(str(item.get("pnl", "0")))
            cumulative_pnl = Decimal(
                str(cumulative_values[trade_number - 1][1])
            )
            direction = side_text.get(
                str(item.get("side", "")), str(item.get("side", ""))
            )
            reason = exit_text.get(
                str(item.get("exit_reason", "")),
                str(item.get("exit_reason", "")),
            )
            color = (
                COLORS["positive"]
                if pnl > 0
                else COLORS["negative"]
                if pnl < 0
                else COLORS["text"]
            )
            state = "已选择" if clicked else "当前悬停"
            guide.clear()
            guide.append(trade_number, axis_y.min())
            guide.append(trade_number, axis_y.max())
            guide.setVisible(True)
            detail.setText(
                f"<b>{state}：第 {trade_number} 笔 · {direction}</b>　"
                f"开仓 {self._backtest_datetime(item.get('entry_time'))} "
                f"@ {self._backtest_metric(item.get('entry_price', '0'))}　"
                f"平仓 {self._backtest_datetime(item.get('exit_time'))} "
                f"@ {self._backtest_metric(item.get('exit_price', '0'))}<br>"
                f"数量 {self._backtest_metric(item.get('quantity', '0'))}　"
                f"单笔盈亏 <span style='color:{color}; font-weight:600'>"
                f"{self._backtest_metric(pnl)} {currency}</span>　"
                f"累计盈亏 {self._backtest_metric(cumulative_pnl)} {currency}　"
                f"退出原因：{reason}"
            )
            QToolTip.showText(
                QCursor.pos(),
                f"第 {trade_number} 笔 · {direction}\n"
                f"单笔盈亏 {self._backtest_metric(pnl)} {currency}\n"
                f"累计盈亏 {self._backtest_metric(cumulative_pnl)} {currency}\n"
                f"退出原因：{reason}",
                view,
            )

        def hover_point(point: QPointF, hovered: bool) -> None:
            if hovered:
                show_point(point, clicked=False)
            else:
                QToolTip.hideText()

        for series in (curve, wins, losses):
            series.hovered.connect(hover_point)
            series.clicked.connect(
                lambda point, _series=series: show_point(point, clicked=True)
            )
        view.set_point_callback(
            lambda point, clicked: show_point(point, clicked=clicked)
        )
        return view, detail

    def _show_backtest_trade_detail_dialog(
        self,
        summary: dict[str, object],
        items: list[dict[str, object]],
    ) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(
            f"回测明细 - {summary.get('symbol', '')}"
        )
        dialog.resize(1180, 680)
        layout = QVBoxLayout(dialog)
        provider = str(summary.get("provider", ""))
        currency = "USDT" if provider == "binance_futures" else "USDC"
        total_pnl = self._backtest_metric(summary.get("total_pnl", "0"))
        overview = QLabel(
            f"标的：{summary.get('symbol', '')}    "
            f"策略：{summary.get('strategy', '')}    "
            f"交易：{summary.get('trade_count', len(items))} 笔    "
            f"胜/负：{summary.get('win_count', 0)}/{summary.get('loss_count', 0)}    "
            f"总盈亏：{total_pnl} {currency}    "
            f"收益率：{self._backtest_metric(summary.get('return_percent', '0'), '%')}    "
            f"最大回撤：{self._backtest_metric(summary.get('max_drawdown_percent', '0'), '%')}"
        )
        overview.setWordWrap(True)
        overview.setStyleSheet(f"color: {COLORS['muted']};")
        layout.addWidget(overview)

        chart_view, chart_detail = self._build_backtest_pnl_chart(
            items, currency
        )

        table = KeyedTable(
            [
                "开仓时间", "平仓时间", "方向", "开仓价", "平仓价",
                "数量", "盈亏", "退出原因", "信号原因",
            ],
            [165, 165, 65, 85, 85, 80, 90, 95, 330],
            multi_select=False,
        )
        side_text = {"LONG": "多头", "SHORT": "空头"}
        exit_text = {
            "STOP_LOSS": "止损",
            "TAKE_PROFIT": "止盈",
            "END_OF_DATA": "数据结束",
        }
        for index, item in enumerate(items):
            pnl = Decimal(str(item.get("pnl", "0")))
            table.insert(
                "",
                None,
                iid=str(item.get("trade_id", index)),
                text=self._backtest_datetime(item.get("entry_time")),
                values=(
                    self._backtest_datetime(item.get("exit_time")),
                    side_text.get(str(item.get("side", "")), str(item.get("side", ""))),
                    self._backtest_metric(item.get("entry_price", "0")),
                    self._backtest_metric(item.get("exit_price", "0")),
                    self._backtest_metric(item.get("quantity", "0")),
                    f"{self._backtest_metric(pnl)} {currency}",
                    exit_text.get(
                        str(item.get("exit_reason", "")),
                        str(item.get("exit_reason", "")),
                    ),
                    item.get("signal_reason", ""),
                ),
                tags=("win",) if pnl > 0 else ("loss",) if pnl < 0 else (),
            )
            row = table.rowCount() - 1
            for column in range(table.columnCount()):
                cell = table.item(row, column)
                header = table.horizontalHeaderItem(column)
                if cell is not None:
                    title = header.text() if header is not None else "明细"
                    cell.setToolTip(f"{title}：{cell.text()}")
        table.setMouseTracking(True)
        table.viewport().setMouseTracking(True)
        pages = QTabWidget(dialog)
        pages.setDocumentMode(True)
        chart_page = QWidget(pages)
        chart_layout = QVBoxLayout(chart_page)
        chart_layout.setContentsMargins(6, 6, 6, 6)
        chart_layout.addWidget(chart_view, 1)
        chart_layout.addWidget(chart_detail)
        table_page = QWidget(pages)
        table_layout = QVBoxLayout(table_page)
        table_layout.setContentsMargins(6, 6, 6, 6)
        table_layout.addWidget(table, 1)
        if not items:
            empty = QLabel("该回测没有产生交易明细。")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {COLORS['muted']};")
            table_layout.addWidget(empty)
        pages.addTab(chart_page, "收益曲线")
        pages.addTab(table_page, f"交易明细 ({len(items)})")
        layout.addWidget(pages, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        self._backtest_detail_dialog = dialog
        dialog.show()

    def _apply_backtest_data(
        self,
        downloads: list[dict[str, object]],
        runs: list[dict[str, object]],
        error: str,
    ) -> None:
        self._backtest_refresh_inflight = False
        self.backtest_refresh_button.setEnabled(True)
        if error:
            self.backtest_status_var.set(f"回测记录刷新失败：{error}")
            return
        status_text = {
            "QUEUED": "排队中",
            "RUNNING": "进行中",
            "COMPLETED": "已完成",
            "FAILED": "失败",
        }
        self._backtest_downloads = {
            str(item.get("download_id", "")): dict(item)
            for item in downloads
            if str(item.get("download_id", ""))
        }
        self.backtest_download_tree.clear_rows()
        for item in downloads:
            key = str(item.get("download_id", ""))
            status = str(item.get("status", ""))
            self.backtest_download_tree.insert(
                "", None, iid=key, text=self._backtest_datetime(item.get("created_at")),
                values=(
                    item.get("symbol", ""), item.get("provider", ""),
                    status_text.get(status, status), f"{item.get('progress', 0)}%",
                    item.get("daily_count", 0), item.get("five_minute_count", 0),
                    item.get("one_minute_count", 0), item.get("message", ""),
                ),
                tags=("error",) if status == "FAILED" else ("running",) if status == "RUNNING" else (),
            )
        self.backtest_run_tree.clear_rows()
        for item in runs:
            key = str(item.get("run_id", ""))
            status = str(item.get("status", ""))
            provider = str(item.get("provider", ""))
            currency = (
                "USDT"
                if provider == "binance_futures"
                else "USDC"
                if provider == "binance_stocks"
                else ""
            )
            total_pnl = self._backtest_metric(item.get("total_pnl", "0"))
            if currency:
                total_pnl += f" {currency}"
            self.backtest_run_tree.insert(
                "", None, iid=key,
                text=self._backtest_datetime(
                    item.get("completed_at") or item.get("created_at")
                ),
                values=(
                    item.get("symbol", ""), item.get("strategy", ""),
                    status_text.get(status, status), item.get("trade_count", 0),
                    f"{item.get('win_count', 0)}/{item.get('loss_count', 0)}",
                    total_pnl,
                    self._backtest_metric(item.get("return_percent", "0"), "%"),
                    self._backtest_metric(
                        item.get("max_drawdown_percent", "0"), "%"
                    ),
                    "",
                    item.get("message", ""),
                ),
                tags=("error",) if status == "FAILED" else ("running",) if status == "RUNNING" else (),
            )
            detail_button = QPushButton("回测明细", self.backtest_run_tree)
            detail_button.setFlat(True)
            detail_button.setCursor(Qt.CursorShape.PointingHandCursor)
            detail_button.setStyleSheet(
                f"""
                QPushButton {{
                    min-height: 24px;
                    padding: 0;
                    color: {COLORS['primary']};
                    background: transparent;
                    border: none;
                    font-weight: 600;
                }}
                QPushButton:hover {{
                    color: {COLORS['primary_hover']};
                    background: transparent;
                    border: none;
                    text-decoration: underline;
                }}
                QPushButton:pressed {{
                    color: {COLORS['primary_hover']};
                    background: transparent;
                    border: none;
                }}
                QPushButton:disabled {{
                    color: #98a2b3;
                    background: transparent;
                    border: none;
                }}
                """
            )
            detail_button.setEnabled(status == "COMPLETED")
            detail_button.setToolTip("查看该回测批次的逐笔开平仓明细")
            detail_button.clicked.connect(
                lambda _checked=False, run_id=key, summary=dict(item):
                self._load_backtest_trade_details(run_id, summary)
            )
            self.backtest_run_tree.setCellWidget(
                self.backtest_run_tree.rowCount() - 1, 9, detail_button
            )
        active_downloads = sum(
            1 for item in downloads if item.get("status") in {"QUEUED", "RUNNING"}
        )
        active_runs = sum(
            1 for item in runs if item.get("status") in {"QUEUED", "RUNNING"}
        )
        self.backtest_status_var.set(
            f"已加载 {len(downloads)} 条下载记录、{len(runs)} 条回测结果；"
            f"进行中的下载 {active_downloads} 个、回测 {active_runs} 个。"
        )

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
            elif event[0] == "trade_history":
                self._apply_trade_history(event[1])
            elif event[0] == "trade_history_error":
                self._apply_trade_history_error(event[1])
            elif event[0] == "ai_decisions":
                self._apply_ai_decisions(event[1])
            elif event[0] == "ai_decisions_error":
                self._apply_ai_decisions_error(event[1])
            elif event[0] == "futures_rankings":
                self._apply_futures_rankings(event[1], event[2])
            elif event[0] == "contract_pool_tickers":
                self._apply_contract_pool_tickers(event[1], event[2])
            elif event[0] == "backtest_action":
                self._apply_backtest_action(event[1], event[2], event[3])
            elif event[0] == "backtest_data":
                self._apply_backtest_data(event[1], event[2], event[3])
            elif event[0] == "backtest_archive":
                self._apply_backtest_archive(
                    event[1], event[2], event[3], event[4]
                )
            elif event[0] == "backtest_delete":
                self._apply_backtest_delete(event[1], event[2])
            elif event[0] == "backtest_details":
                self._apply_backtest_trade_details(
                    event[1], event[2], event[3]
                )
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

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closed = True
        self.event_timer.stop()
        self.account_timer.stop()
        self.backtest_timer.stop()
        self.futures_rankings_timer.stop()
        self.contract_pool_timer.stop()
        self.controller.close()
        event.accept()


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AutoQuant")
    app.setOrganizationName("AutoQuant")
    app.setWindowIcon(QIcon(str(application_icon_path())))
    window = AutoQuantApp()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()

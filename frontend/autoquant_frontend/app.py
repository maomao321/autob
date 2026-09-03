from __future__ import annotations

import queue
import sys
from decimal import Decimal
from typing import Callable

from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QWidget,
)

from autoquant_shared.config import (
    AppConfig,
    ConfigStore,
    credential_or_environment,
)
from autoquant_frontend.services.client import (
    BacktestStatusListener,
    BackendClient,
    RemoteConfigStore,
    RemoteTradingController,
)
from autoquant_frontend.components import (
    InteractiveChartView,
    KeyedTable,
    TextValue,
    show_error,
    show_info,
    show_warning,
)
from autoquant_frontend.pages import (
    AiDecisionPageMixin,
    BacktestPageMixin,
    ConfigPageMixin,
    ContractPoolPageMixin,
    ExperiencePageMixin,
    StrategyConfigPageMixin,
    TradeHistoryPageMixin,
    TradingPageMixin,
    UsersPageMixin,
)
from autoquant_frontend.pages.users import authenticate_client
from autoquant_frontend.ui import (
    ACCOUNT_REFRESH_MS,
    COLORS,
    CONTRACT_POOL_REFRESH_MS,
    FUTURES_RANKINGS_REFRESH_MS,
    application_icon_path,
    application_style_sheet,
)
from autoquant_frontend.services.experience import TradeExperience
from autoquant_shared.models import AiDecisionHistoryItem


__all__ = [
    "AutoQuantApp",
    "COLORS",
    "InteractiveChartView",
    "KeyedTable",
    "TextValue",
    "application_icon_path",
    "main",
]


class AutoQuantApp(
    UsersPageMixin,
    ContractPoolPageMixin,
    ConfigPageMixin,
    StrategyConfigPageMixin,
    TradeHistoryPageMixin,
    AiDecisionPageMixin,
    ExperiencePageMixin,
    BacktestPageMixin,
    TradingPageMixin,
    QMainWindow,
):
    def __init__(
        self,
        config_store: ConfigStore | RemoteConfigStore | None = None,
        backend_client: BackendClient | None = None,
        controller: RemoteTradingController | None = None,
        authenticated_user: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self.setWindowIcon(QIcon(str(application_icon_path())))
        self.setWindowTitle("AutoQuant - Binance Stocks 量化控制台")
        self.resize(1280, 820)
        self.setMinimumSize(1020, 680)
        self.backend_client = backend_client or BackendClient()
        self.authenticated_user = authenticated_user or {}
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
        self.buy_notional_var = TextValue(self.config.buy_notional)
        self.max_additions_var = TextValue(
            str(self.config.max_additions_per_position)
        )
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
            "打开页面后获取 Binance 股票与加密 USDT 永续合约 24 小时涨跌榜，之后每 30 分钟自动刷新。"
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
        self._backtest_status_listener: BacktestStatusListener | None = None
        self._backtest_downloads: dict[str, dict[str, object]] = {}
        self._backtest_active_runs: dict[str, dict[str, object]] = {}
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
        self.strategy_config_page = QWidget()
        self.trade_history_page = QWidget()
        self.ai_decision_page = QWidget()
        self.experience_page = QWidget()
        self.backtest_page = QWidget()
        self.users_page = QWidget()
        self.notebook.addTab(self.main_page, "交易监控")
        self.notebook.addTab(self.contract_pool_page, "合约池")
        self.notebook.addTab(self.config_page, "运行配置")
        self.notebook.addTab(self.strategy_config_page, "策略配置")
        self.notebook.addTab(self.trade_history_page, "交易记录")
        self.notebook.addTab(self.ai_decision_page, "AI 决策")
        self.notebook.addTab(self.experience_page, "交易经验库")
        self.notebook.addTab(self.backtest_page, "策略回测")
        self.notebook.addTab(self.users_page, "用户管理")
        self._build_main_page()
        self._build_contract_pool_page()
        self._build_config_page()
        self._build_strategy_config_page()
        self._build_trade_history_page()
        self._build_ai_decision_page()
        self._build_experience_page()
        self._build_backtest_page()
        self._build_users_page()
        self.notebook.currentChanged.connect(self._on_page_changed)

    @staticmethod
    def _style_sheet() -> str:
        return application_style_sheet()

    def _on_page_changed(self, _index: int) -> None:
        if self.notebook.currentWidget() is self.backtest_page:
            self._start_backtest_status_listener()
        if self.notebook.currentWidget() is self.contract_pool_page:
            self._sync_contract_pool_refresh_timer()
            self._refresh_futures_rankings()
        else:
            self.contract_pool_timer.stop()

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
                self._apply_backtest_action(
                    event[1],
                    event[2],
                    event[3],
                    event[4] if len(event) > 4 else "",
                )
            elif event[0] == "backtest_stop":
                self._apply_backtest_stop(
                    event[1], event[2], event[3]
                )
            elif event[0] == "backtest_data":
                self._apply_backtest_data(event[1], event[2], event[3])
            elif event[0] == "backtest_status":
                self._apply_backtest_data(
                    event[1],
                    event[2],
                    event[3],
                    refresh_complete=False,
                )
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

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closed = True
        self.event_timer.stop()
        self.account_timer.stop()
        self._stop_backtest_status_listener()
        self.futures_rankings_timer.stop()
        self.contract_pool_timer.stop()
        self.controller.close()
        event.accept()


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AutoQuant")
    app.setOrganizationName("AutoQuant")
    app.setWindowIcon(QIcon(str(application_icon_path())))
    client = BackendClient()
    authenticated_user = authenticate_client(client)
    if authenticated_user is None:
        return
    window = AutoQuantApp(
        backend_client=client, authenticated_user=authenticated_user
    )
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtCharts import QChartView
from PySide6.QtWidgets import (
    QApplication,
    QGroupBox,
    QLabel,
    QLineEdit,
    QTabWidget,
)

from autoquant_frontend.app import (
    AutoQuantApp,
    COLORS,
    InteractiveChartView,
    KeyedTable,
    TextValue,
)
from autoquant_frontend.client import BackendClientError
from autoquant_shared.config import AppConfig, ConfigStore
from autoquant_shared.models import (
    AiDecisionHistoryItem,
    Direction,
    RunState,
    RuntimeSnapshot,
    TradeHistoryItem,
)


class QtAppWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_text_value_synchronizes_with_line_edit(self) -> None:
        value = TextValue("initial")
        field = QLineEdit()
        value.bind_line_edit(field)

        self.assertEqual("initial", field.text())
        field.setText("from-widget")
        self.assertEqual("from-widget", value.get())
        value.set("from-model")
        self.assertEqual("from-model", field.text())

    def test_strategy_configuration_has_its_own_page_and_description(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["AAPL"]))
            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)

            tab_titles = [
                window.notebook.tabText(index)
                for index in range(window.notebook.count())
            ]
            self.assertIn("策略配置", tab_titles)
            self.assertIsNot(window.strategy_config_page, window.config_page)
            self.assertEqual(1, window.strategy_config_tabs.count())
            self.assertEqual(
                "五分钟突破", window.strategy_config_tabs.tabText(0)
            )
            self.assertEqual(
                {"five_minute_breakout"},
                set(window.strategy_config_pages),
            )
            description = window.five_minute_breakout_description.text()
            self.assertIn("做多信号", description)
            self.assertIn("做空信号", description)
            self.assertIn("30 根", description)
            self.assertIsNotNone(
                window.strategy_config_page.findChild(
                    QGroupBox, "fiveMinuteBreakoutSettings"
                )
            )
            strategy_labels = {
                label.text()
                for label in window.strategy_config_page.findChildren(QLabel)
            }
            runtime_labels = {
                label.text()
                for label in window.config_page.findChildren(QLabel)
            }
            for title in (
                "开仓金额(USDC/USDT)",
                "单笔上限(USDC/USDT)",
                "每日开仓上限",
            ):
                self.assertIn(title, strategy_labels)
                self.assertNotIn(title, runtime_labels)
            self.assertNotIn("策略均线", runtime_labels)
            self.assertNotIn("持仓加仓次数", runtime_labels)

    def test_backtest_submits_current_strategy_configuration_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["BTCUSDT"]))
            backend_client = MagicMock()
            backend_client.start_backtest.return_value = "run-1"
            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store, backend_client=backend_client)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window._stop_backtest_status_listener)
            self.addCleanup(window.futures_rankings_timer.stop)
            self.addCleanup(window.contract_pool_timer.stop)
            self.addCleanup(window.deleteLater)
            window.backtest_symbol_var.set("BTCUSDT")
            window.buy_notional_var.set("125")
            window.max_order_notional_var.set("200")
            window.max_daily_buy_notional_var.set("500")
            window.max_additions_var.set("3")

            with patch("autoquant_frontend.app.threading.Thread") as thread:
                window._start_backtest_run()
                thread.call_args.kwargs["target"]()

            snapshot = backend_client.start_backtest.call_args.args[2]
            self.assertEqual("125", snapshot["buy_notional"])
            self.assertEqual("200", snapshot["max_order_notional"])
            self.assertEqual("500", snapshot["max_daily_buy_notional"])
            self.assertEqual(3, snapshot["max_additions_per_position"])
            self.assertNotIn("api_key", snapshot)

    def test_download_row_can_start_stop_backtest_and_update_klines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(
                AppConfig(
                    symbols=["BTCUSDT"], provider="binance_futures"
                )
            )
            backend_client = MagicMock()
            backend_client.start_backtest.return_value = "run-1"
            backend_client.update_historical_download.return_value = "download-2"
            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store, backend_client=backend_client)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window._stop_backtest_status_listener)
            self.addCleanup(window.futures_rankings_timer.stop)
            self.addCleanup(window.contract_pool_timer.stop)
            self.addCleanup(window.deleteLater)
            download = {
                "download_id": "download-1",
                "created_at": 1_700_000_000_000,
                "updated_at": 1_700_000_300_000,
                "symbol": "BTCUSDT",
                "provider": "binance_futures",
                "status": "COMPLETED",
                "progress": 100,
                "daily_count": 100,
                "five_minute_count": 1000,
                "one_minute_count": 5000,
                "message": "下载完成",
            }
            window._apply_backtest_data([download], [], "")

            self.assertEqual(
                "BTCUSDT", window.backtest_download_tree.item(0, 0).text()
            )
            self.assertEqual(
                window._backtest_datetime(1_700_000_300_000),
                window.backtest_download_tree.item(0, 3).text(),
            )
            self.assertEqual(
                "回测",
                window.backtest_download_tree.horizontalHeaderItem(1).text(),
            )
            action_button = window.backtest_download_tree.action_button(
                "download-1"
            )
            self.assertEqual("start", action_button.property("action"))
            self.assertEqual("启动回测 BTCUSDT", action_button.toolTip())

            with (
                patch(
                    "autoquant_frontend.app.QInputDialog.getItem",
                    return_value=("五分钟突破", True),
                ),
                patch("autoquant_frontend.app.threading.Thread") as thread,
            ):
                action_button.click()
                thread.call_args.kwargs["target"]()

            self.assertEqual(
                "download-1",
                backend_client.start_backtest.call_args.kwargs["download_id"],
            )
            self.assertEqual(
                "binance_futures",
                backend_client.start_backtest.call_args.kwargs["provider"],
            )
            window._apply_backtest_action(
                "run", "run-1", "", "download-1"
            )
            active_run = {
                "run_id": "run-1",
                "download_id": "download-1",
                "created_at": 1_700_000_400_000,
                "symbol": "BTCUSDT",
                "provider": "binance_futures",
                "strategy": "five_minute_breakout",
                "status": "RUNNING",
                "trade_count": 0,
                "win_count": 0,
                "loss_count": 0,
                "total_pnl": "0",
                "return_percent": "0",
                "max_drawdown_percent": "0",
                "message": "正在执行回测",
            }
            window._apply_backtest_data([download], [active_run], "")
            action_button = window.backtest_download_tree.action_button(
                "download-1"
            )
            self.assertEqual("stop", action_button.property("action"))
            self.assertEqual("停止回测 BTCUSDT", action_button.toolTip())

            with patch("autoquant_frontend.app.threading.Thread") as thread:
                action_button.click()
                thread.call_args.kwargs["target"]()
            backend_client.stop_backtest.assert_called_once_with("run-1")
            window._apply_backtest_stop("run-1", "download-1", "")

            with patch("autoquant_frontend.app.threading.Thread") as thread:
                window._update_historical_bars("download-1", "BTCUSDT")
                thread.call_args.kwargs["target"]()
            backend_client.update_historical_download.assert_called_once_with(
                "download-1"
            )

    def test_ai_switch_controls_runner_direction_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(
                AppConfig(
                    symbols=["AAPL"],
                    ai_provider="CHATGPT",
                    openai_api_key="saved-openai-key",
                    qwen_api_key="saved-qwen-key",
                    openai_reasoning_enabled=True,
                    openai_reasoning_effort="high",
                    qwen_thinking_enabled=True,
                    qwen_reasoning_effort="xhigh",
                    ai_entry_timing_bars=90,
                    deepseek_thinking_enabled=False,
                    deepseek_reasoning_effort="high",
                    manual_directions={"AAPL": "LONG"},
                )
            )
            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)

            self.assertTrue(window.ai_enabled_checkbox.isChecked())
            self.assertEqual("saved-openai-key", window.openai_api_key_var.get())
            self.assertEqual("saved-qwen-key", window.qwen_api_key_var.get())
            self.assertTrue(window.openai_reasoning_checkbox.isChecked())
            self.assertEqual("high", window.openai_reasoning_effort_var.get())
            self.assertEqual("90", window.ai_entry_timing_bars_var.get())
            self.assertFalse(window.deepseek_thinking_checkbox.isChecked())
            self.assertEqual("high", window.deepseek_reasoning_effort_var.get())
            window._start_symbols(["AAPL"])
            enabled_config = window.controller.start.call_args.args[1]
            self.assertEqual(
                Direction.UNKNOWN, enabled_config.manual_direction
            )
            self.assertEqual("CHATGPT", enabled_config.app.ai_provider)
            self.assertFalse(window.openai_settings_group.isHidden())
            self.assertTrue(window.deepseek_settings_group.isHidden())
            self.assertTrue(window.qwen_settings_group.isHidden())
            self.assertFalse(enabled_config.app.deepseek_thinking_enabled)
            self.assertEqual(
                "high", enabled_config.app.deepseek_reasoning_effort
            )
            self.assertEqual(90, enabled_config.app.ai_entry_timing_bars)
            self.assertEqual(
                "saved-openai-key", enabled_config.app.openai_api_key
            )

            window.controller.start.reset_mock()
            window.ai_provider_var.set("QWEN")
            self.assertTrue(window.openai_settings_group.isHidden())
            self.assertTrue(window.deepseek_settings_group.isHidden())
            self.assertFalse(window.qwen_settings_group.isHidden())
            window._start_symbols(["AAPL"])
            qwen_config = window.controller.start.call_args.args[1]
            self.assertEqual("QWEN", qwen_config.app.ai_provider)
            self.assertEqual("saved-qwen-key", qwen_config.qwen_api_key)
            self.assertEqual("qwen-plus", qwen_config.app.qwen_model)
            self.assertTrue(qwen_config.app.qwen_thinking_enabled)
            self.assertEqual("xhigh", qwen_config.app.qwen_reasoning_effort)

            window.ai_provider_var.set("DUAL")
            self.assertFalse(window.openai_settings_group.isHidden())
            self.assertFalse(window.deepseek_settings_group.isHidden())
            self.assertTrue(window.qwen_settings_group.isHidden())

            window.controller.start.reset_mock()
            window.ai_enabled_checkbox.setChecked(False)
            window._start_symbols(["AAPL"])
            disabled_config = window.controller.start.call_args.args[1]
            self.assertEqual(Direction.LONG, disabled_config.manual_direction)
            self.assertEqual("DISABLED", disabled_config.app.ai_provider)

    def test_financial_display_uses_two_places_without_rounding_quantity(self) -> None:
        self.assertEqual("123.46", AutoQuantApp._format_decimal(Decimal("123.456"), 2))
        self.assertEqual("0.123456", AutoQuantApp._format_decimal(Decimal("0.123456")))

    def test_trade_history_page_displays_persisted_record_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["AAPL"]))
            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)

            window._apply_trade_history(
                [
                    TradeHistoryItem(
                        executed_at=1_700_000_000_000,
                        symbol="AAPL",
                        action="CLOSE",
                        opening_direction="LONG",
                        price=Decimal("110"),
                        quantity=Decimal("2"),
                        amount=Decimal("220"),
                        fee=Decimal("1"),
                        profit=Decimal("19"),
                        paper=True,
                    )
                ]
            )

        self.assertEqual(1, window.trade_history_tree.rowCount())
        self.assertEqual("AAPL", window.trade_history_tree.item(0, 1).text())
        self.assertEqual("平仓", window.trade_history_tree.item(0, 2).text())
        self.assertEqual("多头", window.trade_history_tree.item(0, 3).text())
        self.assertEqual("110.00", window.trade_history_tree.item(0, 4).text())
        self.assertEqual("19.00", window.trade_history_tree.item(0, 8).text())
        self.assertIn("平仓收益合计 19.00", window.trade_history_status_var.get())

    def test_backtest_download_table_supports_row_context_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["BTCUSDT"], provider="binance_futures"))
            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window._stop_backtest_status_listener)
            self.addCleanup(window.futures_rankings_timer.stop)
            self.addCleanup(window.contract_pool_timer.stop)
            self.addCleanup(window.deleteLater)

            self.assertEqual(30 * 60 * 1000, window.futures_rankings_timer.interval())
            self.assertEqual(60 * 1000, window.contract_pool_timer.interval())
            self.assertFalse(window.contract_pool_timer.isActive())
            self.assertEqual(4, window.futures_ranking_tabs.count())
            self.assertEqual("股票涨幅榜", window.futures_ranking_tabs.tabText(0))
            self.assertEqual("股票跌幅榜", window.futures_ranking_tabs.tabText(1))
            self.assertEqual("加密涨幅榜", window.futures_ranking_tabs.tabText(2))
            self.assertEqual("加密跌幅榜", window.futures_ranking_tabs.tabText(3))
            self.assertFalse(
                any(
                    group.title() == "合约池"
                    for group in window.contract_pool_page.findChildren(QGroupBox)
                )
            )
            for table in (
                window.stock_gainers_tree,
                window.stock_losers_tree,
                window.crypto_gainers_tree,
                window.crypto_losers_tree,
            ):
                self.assertEqual(
                    Qt.ContextMenuPolicy.CustomContextMenu,
                    table.contextMenuPolicy(),
                )
            self.assertFalse(hasattr(window, "contract_pool_add_button"))
            with patch.object(window, "_refresh_futures_rankings") as refresh:
                window.notebook.setCurrentWidget(window.contract_pool_page)
                self.assertFalse(window.contract_pool_timer.isActive())
                refresh.assert_called_once()
                window.notebook.setCurrentWidget(window.main_page)
                self.assertFalse(window.contract_pool_timer.isActive())

            window._apply_backtest_data(
                [
                    {
                        "download_id": "download-1",
                        "created_at": 1_700_000_000_000,
                        "symbol": "BTCUSDT",
                        "provider": "binance_futures",
                        "status": "COMPLETED",
                        "progress": 100,
                        "daily_count": 100,
                        "five_minute_count": 1000,
                        "one_minute_count": 5000,
                        "message": "下载完成",
                    }
                ],
                [
                    {
                        "run_id": "run-1",
                        "created_at": 1_700_000_000_000,
                        "completed_at": 1_700_000_100_000,
                        "symbol": "BTCUSDT",
                        "provider": "binance_futures",
                        "strategy": "five_minute_breakout",
                        "status": "COMPLETED",
                        "trade_count": 270,
                        "win_count": 90,
                        "loss_count": 180,
                        "total_pnl": "1.4870887",
                        "return_percent": "1.4870887",
                        "max_drawdown_percent": "34.944173",
                        "strategy_config": {
                            "strategy": "five_minute_breakout",
                            "strategy_name": "五分钟突破",
                            "kline_interval": "5m",
                            "fast_ma_period": 7,
                            "slow_ma_period": 25,
                            "buy_notional": "125",
                            "max_order_notional": "200",
                            "max_daily_buy_notional": "500",
                            "max_additions_per_position": 3,
                            "stop_loss_percent": "1.5",
                            "take_profit_percent": "5",
                            "max_signal_age_seconds": 45,
                            "ai_entry_timing_bars": 80,
                        },
                        "message": "回测完成",
                    }
                ],
                "",
            )
            window._show_backtest_trade_detail_dialog(
                {
                    "symbol": "BTCUSDT",
                    "provider": "binance_futures",
                    "strategy": "five_minute_breakout",
                    "trade_count": 1,
                    "win_count": 1,
                    "loss_count": 0,
                    "total_pnl": "1.487",
                    "return_percent": "1.487",
                    "max_drawdown_percent": "0",
                    "strategy_config": {
                        "strategy": "five_minute_breakout",
                        "strategy_name": "五分钟突破",
                        "kline_interval": "5m",
                        "fast_ma_period": 7,
                        "slow_ma_period": 25,
                        "buy_notional": "125",
                        "max_order_notional": "200",
                        "max_daily_buy_notional": "500",
                        "max_additions_per_position": 3,
                        "stop_loss_percent": "1.5",
                        "take_profit_percent": "5",
                        "max_signal_age_seconds": 45,
                        "ai_entry_timing_bars": 80,
                    },
                },
                [
                    {
                        "trade_id": 1,
                        "side": "LONG",
                        "entry_time": 1_700_000_000_000,
                        "exit_time": 1_700_000_300_000,
                        "entry_price": "12.34",
                        "exit_price": "12.52",
                        "quantity": "8.12",
                        "pnl": "1.49",
                        "exit_reason": "TAKE_PROFIT",
                        "signal_reason": "五分钟突破",
                    }
                ],
            )

        self.assertEqual(
            Qt.ContextMenuPolicy.CustomContextMenu,
            window.backtest_download_tree.contextMenuPolicy(),
        )
        self.assertEqual(
            "BTCUSDT", window._backtest_downloads["download-1"]["symbol"]
        )
        self.assertEqual("1.49 USDT", window.backtest_run_tree.item(0, 6).text())
        self.assertEqual("1.49%", window.backtest_run_tree.item(0, 7).text())
        self.assertEqual("34.94%", window.backtest_run_tree.item(0, 8).text())
        detail_link = window.backtest_run_tree.cellWidget(0, 9)
        self.assertEqual("回测明细", detail_link.text())
        self.assertTrue(detail_link.isFlat())
        self.assertTrue(detail_link.icon().isNull())
        self.assertEqual(
            Qt.CursorShape.PointingHandCursor, detail_link.cursor().shape()
        )
        self.assertIn("background: transparent", detail_link.styleSheet())
        self.assertIn("border: none", detail_link.styleSheet())
        self.assertEqual(
            "回测明细 - BTCUSDT", window._backtest_detail_dialog.windowTitle()
        )
        chart_views = window._backtest_detail_dialog.findChildren(QChartView)
        self.assertEqual(1, len(chart_views))
        self.assertIsInstance(chart_views[0], InteractiveChartView)
        self.assertTrue(chart_views[0].hasMouseTracking())
        self.assertTrue(chart_views[0].viewport().hasMouseTracking())
        self.assertGreaterEqual(len(chart_views[0].chart().series()), 3)
        pages = window._backtest_detail_dialog.findChild(QTabWidget)
        self.assertEqual(3, pages.count())
        self.assertEqual("收益曲线", pages.tabText(0))
        self.assertEqual("交易明细 (1)", pages.tabText(1))
        self.assertEqual("策略配置副本", pages.tabText(2))
        snapshot_note = window._backtest_detail_dialog.findChild(
            QLabel, "backtestStrategyConfigNote"
        )
        self.assertIsNotNone(snapshot_note)
        self.assertIn("不会改变", snapshot_note.text())
        chart_views[0].chart().series()[0].clicked.emit(QPointF(1, 1.49))
        self.qt_app.processEvents()
        chart_detail = window._backtest_detail_dialog.findChild(
            QLabel, "backtestChartDetail"
        )
        self.assertIn("第 1 笔", chart_detail.text())
        self.assertIn("累计盈亏", chart_detail.text())
        plot_area = chart_views[0].chart().plotArea()
        chart_views[0]._dispatch_chart_position(
            plot_area.center(), clicked=False
        )
        self.assertIn("当前悬停", chart_detail.text())
        detail_tables = window._backtest_detail_dialog.findChildren(KeyedTable)
        self.assertEqual(1, len(detail_tables))
        self.assertIn("信号原因", detail_tables[0].item(0, 8).toolTip())

    def test_ai_decision_page_displays_result_input_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["SOXLUSDT"]))
            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)

            window._apply_ai_decisions(
                [
                    AiDecisionHistoryItem(
                        record_id="decision-ui-1",
                        decided_at=1_700_000_000_000,
                        symbol="SOXLUSDT",
                        stage="OPENING_DIRECTION",
                        provider="DEEPSEEK",
                        model="deepseek-v4-pro",
                        outcome="LONG",
                        confidence=0.72,
                        summary="短线动能偏多",
                        factors=("接近日内高点",),
                        risks=("三倍杠杆波动",),
                        input_json='{"context":{"symbol":"SOXLUSDT"}}',
                        output_json=(
                            '[{"model":"deepseek-v4-pro",'
                            '"response":{"direction":"LONG"}}]'
                        ),
                        fallback=False,
                        elapsed_ms=7000,
                        response_ms=6500,
                    )
                ]
            )

        self.assertEqual(1, window.ai_decision_tree.rowCount())
        self.assertEqual("SOXLUSDT", window.ai_decision_tree.item(0, 1).text())
        self.assertEqual("今日方向", window.ai_decision_tree.item(0, 2).text())
        self.assertEqual("72%", window.ai_decision_tree.item(0, 6).text())
        self.assertIn("短线动能偏多", window.ai_decision_result_detail.toPlainText())
        self.assertIn("模型响应时间：6500 ms", window.ai_decision_result_detail.toPlainText())
        self.assertIn("SOXLUSDT", window.ai_decision_input_detail.toPlainText())
        self.assertIn("deepseek-v4-pro", window.ai_decision_output_detail.toPlainText())

    def test_keyed_table_keeps_symbol_identity_when_values_change(self) -> None:
        table = KeyedTable(["股票", "状态", "信息"], [80, 90, 180], multi_select=True)
        table.insert("", None, iid="AAPL", text="AAPL", values=("已停止", "未启动"))

        table.item_update("AAPL", values=("运行中", "行情已连接"), tags=("running",))

        self.assertTrue(table.exists("AAPL"))
        self.assertEqual(("AAPL",), table.get_children())
        self.assertEqual("运行中", table.item(0, 1).text())
        self.assertEqual("行情已连接", table.item(0, 2).text())

    def test_keyed_table_exposes_per_row_combo_value(self) -> None:
        table = KeyedTable(
            ["股票", "手动方向"], [80, 100], multi_select=True
        )
        table.insert("", None, iid="AAPL", text="AAPL", values=("AUTO",))
        combo = table.set_combo(
            "AAPL", 1, ("AUTO", "LONG", "SHORT", "FLAT"), "SHORT"
        )

        self.assertEqual("SHORT", table.combo_text("AAPL", 1))
        combo.setCurrentText("LONG")
        self.assertEqual("LONG", table.combo_text("AAPL", 1))

    def test_monitor_table_shows_symbol_profit_and_row_action_icons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["AAPL"]))
            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)

            headers = [
                window.tree.horizontalHeaderItem(column).text()
                for column in range(window.tree.columnCount())
            ]
            self.assertIn("已实现收益", headers)
            self.assertIn("未实现收益", headers)
            self.assertIn("操作", headers)
            self.assertNotIn("MA", headers)
            self.assertNotIn("实时K线", headers)
            self.assertNotIn("今日交易", headers)
            self.assertNotIn("今日开仓金额", headers)
            self.assertIn("开仓金额", headers)

            action_button = window.tree.action_button("AAPL")
            self.assertEqual("▶", action_button.text())
            self.assertEqual(42, action_button.width())
            self.assertGreaterEqual(action_button.height(), 32)
            self.assertIn(COLORS["positive"], action_button.styleSheet())
            self.assertIn("border: none", action_button.styleSheet())
            self.assertIn("border-radius: 5px", action_button.styleSheet())
            self.assertIn("background: #e8f5ec", action_button.styleSheet())
            self.assertTrue(action_button.isEnabled())
            with patch.object(window, "_start_symbols") as start_symbols:
                action_button.click()
            start_symbols.assert_called_once_with(["AAPL"])

            window._apply_snapshot(
                RuntimeSnapshot(
                    symbol="AAPL",
                    state=RunState.RUNNING,
                    direction=Direction.LONG,
                    last_price=Decimal("110"),
                    position_quantity=Decimal("1.236"),
                    realized_pnl=Decimal("4.25"),
                    unrealized_pnl=Decimal("5.25"),
                    profit=Decimal("9.5"),
                    session_open_notional=Decimal("100"),
                    message="运行中",
                )
            )

            self.assertEqual("4.25", window.tree.item(0, 5).text())
            self.assertEqual("5.25", window.tree.item(0, 6).text())
            self.assertEqual("1.24", window.tree.item(0, 7).text())
            self.assertEqual("100.00", window.tree.item(0, 10).text())
            self.assertEqual("●", action_button.text())
            self.assertIn(COLORS["negative"], action_button.styleSheet())
            self.assertIn("background: #fdecea", action_button.styleSheet())
            self.assertTrue(action_button.isEnabled())
            with patch.object(window, "_stop_symbols") as stop_symbols:
                action_button.click()
            stop_symbols.assert_called_once_with(["AAPL"])

            window._apply_snapshot(
                RuntimeSnapshot(
                    symbol="AAPL",
                    state=RunState.RUNNING,
                    direction=Direction.LONG,
                    last_price=Decimal("112"),
                    position_quantity=Decimal("1.236"),
                    realized_pnl=Decimal("4.25"),
                    unrealized_pnl=Decimal("7.75"),
                )
            )
            self.assertEqual("4.25", window.tree.item(0, 5).text())
            self.assertEqual("7.75", window.tree.item(0, 6).text())

            self.assertTrue(window.start_selected_button.isHidden())
            self.assertTrue(window.stop_selected_button.isHidden())
            self.assertTrue(window.start_all_button.isHidden())
            self.assertTrue(window.stop_all_button.isHidden())
            self.assertEqual(
                Qt.ContextMenuPolicy.NoContextMenu,
                window.tree.contextMenuPolicy(),
            )

    def test_adding_symbol_persists_it_without_saving_other_ui_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["AAPL"], ma_period=5))

            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)

            window.buy_notional_var.set("12")
            window.symbol_var.set(" nvda ")
            with patch("autoquant_frontend.app.show_error") as show_error_mock:
                window._add_symbols()

            persisted = store.load()
            self.assertEqual(["AAPL", "NVDA"], persisted.symbols)
            self.assertEqual("100.00", persisted.buy_notional)
            self.assertEqual(("AAPL", "NVDA"), window.tree.get_children())
            show_error_mock.assert_not_called()

    def test_adding_futures_symbols_appends_usdt_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(
                AppConfig(
                    symbols=["BTCUSDT"],
                    provider="binance_futures",
                )
            )

            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)

            window.provider_var.set("binance_futures")
            window.symbol_var.set(" eth, solusdt ")
            with patch("autoquant_frontend.app.show_error") as show_error_mock:
                window._add_symbols()

            persisted = store.load()
            self.assertEqual(
                ["BTCUSDT", "ETHUSDT", "SOLUSDT"], persisted.symbols
            )
            self.assertEqual(
                ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
                window.tree.get_children(),
            )
            show_error_mock.assert_not_called()

    def test_selected_gainer_is_persisted_to_contract_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["AAPL"], contract_pool=["BTCUSDT"]))
            backend_client = MagicMock()

            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store, backend_client=backend_client)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window._stop_backtest_status_listener)
            self.addCleanup(window.futures_rankings_timer.stop)
            self.addCleanup(window.contract_pool_timer.stop)
            self.addCleanup(window.deleteLater)

            window._apply_futures_rankings(
                {
                    "stock_gainers": [],
                    "stock_losers": [],
                    "crypto_gainers": [
                        {
                            "symbol": "ETHUSDT",
                            "price_change_percent": "6.25",
                            "last_price": "3200",
                            "quote_volume": "1250000",
                        }
                    ],
                    "crypto_losers": [
                        {
                            "symbol": "SOLUSDT",
                            "price_change_percent": "-4.5",
                            "last_price": "150",
                            "quote_volume": "900000",
                        }
                    ],
                    "tickers": {
                        "BTCUSDT": {
                            "symbol": "BTCUSDT",
                            "price_change_percent": "1.2",
                            "last_price": "65000",
                            "quote_volume": "9000000",
                        },
                        "ETHUSDT": {
                            "symbol": "ETHUSDT",
                            "price_change_percent": "6.25",
                            "last_price": "3200",
                            "quote_volume": "1250000",
                        },
                        "SOLUSDT": {
                            "symbol": "SOLUSDT",
                            "price_change_percent": "-4.5",
                            "last_price": "150",
                            "quote_volume": "900000",
                        },
                    },
                    "updated_at": 1_700_000_000_000,
                },
                "",
            )
            window.crypto_gainers_tree.selectRow(0)
            window._add_selected_rankings_to_pool(window.crypto_gainers_tree)

            self.assertEqual(
                ["BTCUSDT", "ETHUSDT"], store.load().contract_pool
            )
            self.assertEqual(
                ("BTCUSDT", "ETHUSDT"),
                window.contract_pool_tree.get_children(),
            )
            self.assertEqual(
                "+6.25%", window.crypto_gainers_tree.item(0, 1).text()
            )

            self.assertEqual(
                "+1.2%", window.contract_pool_tree.item(0, 1).text()
            )
            self.assertEqual(
                "+6.25%", window.contract_pool_tree.item(1, 1).text()
            )
            window._apply_contract_pool_tickers(
                {
                    "BTCUSDT": {
                        "symbol": "BTCUSDT",
                        "price_change_percent": "-2.4",
                        "last_price": "64000",
                        "quote_volume": "9500000",
                    }
                },
                "",
            )
            self.assertEqual(
                "-2.4%", window.contract_pool_tree.item(0, 1).text()
            )
            self.assertEqual(
                Qt.ContextMenuPolicy.CustomContextMenu,
                window.crypto_losers_tree.contextMenuPolicy(),
            )
            self.assertEqual(
                Qt.ContextMenuPolicy.CustomContextMenu,
                window.contract_pool_tree.contextMenuPolicy(),
            )

            window.crypto_losers_tree.selectRow(0)
            window._add_selected_rankings_to_pool(window.crypto_losers_tree)
            self.assertEqual(
                ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                store.load().contract_pool,
            )

            window.contract_pool_tree.selectRow(0)
            window._remove_selected_pool_contracts()
            self.assertEqual(
                ["ETHUSDT", "SOLUSDT"], store.load().contract_pool
            )

    def test_contract_pool_shortcuts_open_symbol_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(
                AppConfig(symbols=["AAPL"], contract_pool=["BTCUSDT"])
            )
            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store, backend_client=MagicMock())
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window._stop_backtest_status_listener)
            self.addCleanup(window.futures_rankings_timer.stop)
            self.addCleanup(window.contract_pool_timer.stop)
            self.addCleanup(window.deleteLater)

            with patch("autoquant_frontend.app.BacktestStatusListener"):
                window._open_contract_backtest("BTCUSDT")

            self.assertIs(window.backtest_page, window.notebook.currentWidget())
            self.assertEqual("BTCUSDT", window.backtest_symbol_var.get())

            window._open_contract_quant("BTCUSDT")

            self.assertIs(window.main_page, window.notebook.currentWidget())
            self.assertEqual(("AAPL", "BTCUSDT"), window.tree.get_children())
            self.assertEqual(("BTCUSDT",), window.tree.selection())
            self.assertEqual(["AAPL", "BTCUSDT"], store.load().symbols)
            window.controller.start.assert_not_called()

    def test_contract_pool_timer_tracks_visible_nonempty_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["AAPL"], contract_pool=[]))
            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store, backend_client=MagicMock())
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window._stop_backtest_status_listener)
            self.addCleanup(window.futures_rankings_timer.stop)
            self.addCleanup(window.contract_pool_timer.stop)
            self.addCleanup(window.deleteLater)

            with patch.object(window, "_refresh_futures_rankings"):
                window.notebook.setCurrentWidget(window.contract_pool_page)
            self.assertFalse(window.contract_pool_timer.isActive())

            window.crypto_gainers_tree.insert(
                "", None, iid="ETHUSDT", text="ETHUSDT", values=("+1%",)
            )
            window.crypto_gainers_tree.selectRow(0)
            window._add_selected_rankings_to_pool(window.crypto_gainers_tree)
            self.assertTrue(window.contract_pool_timer.isActive())

            window.contract_pool_tree.selectRow(0)
            window._remove_selected_pool_contracts()
            self.assertFalse(window.contract_pool_timer.isActive())

    def test_backtest_status_listener_tracks_visible_page_without_flashing_button(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["AAPL"]))
            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store, backend_client=MagicMock())
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window._stop_backtest_status_listener)
            self.addCleanup(window.futures_rankings_timer.stop)
            self.addCleanup(window.contract_pool_timer.stop)
            self.addCleanup(window.deleteLater)

            self.assertIsNone(window._backtest_status_listener)
            with patch("autoquant_frontend.app.BacktestStatusListener") as listener_type:
                window.notebook.setCurrentWidget(window.backtest_page)
                listener_type.assert_called_once()
                listener_type.return_value.start.assert_called_once_with()
                self.assertIs(
                    listener_type.return_value,
                    window._backtest_status_listener,
                )

                window.notebook.setCurrentWidget(window.main_page)
                listener_type.return_value.close.assert_not_called()
                self.assertIs(
                    listener_type.return_value,
                    window._backtest_status_listener,
                )
                window._stop_backtest_status_listener()
                listener_type.return_value.close.assert_called_once_with()
                self.assertIsNone(window._backtest_status_listener)

            with patch("autoquant_frontend.app.threading.Thread"):
                window._refresh_backtest_data()
                self.assertTrue(window.backtest_refresh_button.isEnabled())
                window._apply_backtest_data([], [], "")

                window._refresh_backtest_data(manual=True)
                self.assertFalse(window.backtest_refresh_button.isEnabled())
                window._apply_backtest_data([], [], "")
                self.assertTrue(window.backtest_refresh_button.isEnabled())

    def test_removing_last_symbol_persists_without_saving_other_ui_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(
                AppConfig(
                    symbols=["AAPL"],
                    manual_directions={"AAPL": "LONG"},
                    ma_period=5,
                )
            )

            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)
            window.controller.stop_targets.return_value = []

            window.buy_notional_var.set("12")
            window.tree.selectRow(0)
            with (
                patch("autoquant_frontend.app.ask_yes_no", return_value=True),
                patch("autoquant_frontend.app.show_error") as show_error_mock,
            ):
                window._remove_selected()

            persisted = store.load()
            self.assertEqual([], persisted.symbols)
            self.assertEqual({}, persisted.manual_directions)
            self.assertEqual("100.00", persisted.buy_notional)
            self.assertEqual((), window.tree.get_children())
            show_error_mock.assert_not_called()

    def test_remove_keeps_ui_row_when_persistence_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["AAPL"]))

            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)
            window.controller.stop_targets.return_value = []
            window.tree.selectRow(0)

            with (
                patch("autoquant_frontend.app.ask_yes_no", return_value=True),
                patch.object(
                    store,
                    "save",
                    side_effect=BackendClientError("backend unavailable"),
                ),
                patch("autoquant_frontend.app.show_error") as show_error_mock,
            ):
                window._remove_selected()

            self.assertEqual(("AAPL",), window.tree.get_children())
            self.assertEqual(["AAPL"], window.config.symbols)
            show_error_mock.assert_called_once()

    def test_remove_cancellation_keeps_symbol_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["AAPL"]))

            with patch("autoquant_frontend.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)
            window.controller.stop_targets.return_value = []
            window.tree.selectRow(0)

            with (
                patch("autoquant_frontend.app.ask_yes_no", return_value=False)
                as ask_yes_no_mock,
                patch.object(store, "save") as save_mock,
            ):
                window._remove_selected()

            ask_yes_no_mock.assert_called_once_with(
                "确认移除标的",
                "即将从交易监控和服务器配置中移除：AAPL。\n\n"
                "历史交易记录不会被删除。确认继续吗？",
            )
            save_mock.assert_not_called()
            self.assertEqual(("AAPL",), window.tree.get_children())
            self.assertEqual(["AAPL"], window.config.symbols)


if __name__ == "__main__":
    unittest.main()

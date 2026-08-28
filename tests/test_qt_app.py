from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtCharts import QChartView
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QTabWidget

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
            self.addCleanup(window.backtest_timer.stop)
            self.addCleanup(window.deleteLater)

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
        self.assertEqual(2, pages.count())
        self.assertEqual("收益曲线", pages.tabText(0))
        self.assertEqual("交易明细 (1)", pages.tabText(1))
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

            window.ma_var.set("12")
            window.symbol_var.set(" nvda ")
            with patch("autoquant_frontend.app.show_error") as show_error_mock:
                window._add_symbols()

            persisted = store.load()
            self.assertEqual(["AAPL", "NVDA"], persisted.symbols)
            self.assertEqual(5, persisted.ma_period)
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

            window.ma_var.set("12")
            window.tree.selectRow(0)
            with (
                patch("autoquant_frontend.app.ask_yes_no", return_value=True),
                patch("autoquant_frontend.app.show_error") as show_error_mock,
            ):
                window._remove_selected()

            persisted = store.load()
            self.assertEqual([], persisted.symbols)
            self.assertEqual({}, persisted.manual_directions)
            self.assertEqual(5, persisted.ma_period)
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

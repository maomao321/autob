from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit

from autoquant_frontend.app import AutoQuantApp, COLORS, KeyedTable, TextValue
from autoquant_frontend.client import BackendClientError
from autoquant_shared.config import AppConfig, ConfigStore
from autoquant_shared.models import Direction, RunState, RuntimeSnapshot, TradeHistoryItem


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
                    message="运行中",
                )
            )

            self.assertEqual("4.25", window.tree.item(0, 5).text())
            self.assertEqual("5.25", window.tree.item(0, 6).text())
            self.assertEqual("1.24", window.tree.item(0, 7).text())
            self.assertEqual("●", action_button.text())
            self.assertIn(COLORS["negative"], action_button.styleSheet())
            self.assertIn("background: #fdecea", action_button.styleSheet())
            self.assertTrue(action_button.isEnabled())
            with patch.object(window, "_stop_symbols") as stop_symbols:
                action_button.click()
            stop_symbols.assert_called_once_with(["AAPL"])

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
            with patch("autoquant_frontend.app.show_error") as show_error_mock:
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


if __name__ == "__main__":
    unittest.main()

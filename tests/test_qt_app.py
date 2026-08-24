from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit

from autoquant.app import AutoQuantApp, KeyedTable, TextValue
from autoquant.config import AppConfig, ConfigStore


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

    def test_adding_symbol_persists_it_without_saving_other_ui_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(AppConfig(symbols=["AAPL"], ma_period=5))

            with patch("autoquant.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)

            window.ma_var.set("12")
            window.symbol_var.set(" nvda ")
            with patch("autoquant.app.show_error") as show_error_mock:
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

            with patch("autoquant.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)

            window.provider_var.set("binance_futures")
            window.symbol_var.set(" eth, solusdt ")
            with patch("autoquant.app.show_error") as show_error_mock:
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

            with patch("autoquant.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)
            window.controller.stop_targets.return_value = []

            window.ma_var.set("12")
            window.tree.selectRow(0)
            with patch("autoquant.app.show_error") as show_error_mock:
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

            with patch("autoquant.app.RemoteTradingController"):
                window = AutoQuantApp(store)
            self.addCleanup(window.event_timer.stop)
            self.addCleanup(window.account_timer.stop)
            self.addCleanup(window.deleteLater)
            window.controller.stop_targets.return_value = []
            window.tree.selectRow(0)

            with (
                patch.object(store, "save", side_effect=OSError("disk full")),
                patch("autoquant.app.show_error") as show_error_mock,
            ):
                window._remove_selected()

            self.assertEqual(("AAPL",), window.tree.get_children())
            self.assertEqual(["AAPL"], window.config.symbols)
            show_error_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()

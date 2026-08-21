from __future__ import annotations

import os
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit

from autoquant.app import KeyedTable, TextValue


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


if __name__ == "__main__":
    unittest.main()

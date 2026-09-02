from __future__ import annotations

from typing import Callable

from PySide6.QtCharts import QChart, QChartView
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from autoquant_frontend.ui.theme import COLORS


class TextValue:
    """Small observable text value that can bind common Qt input widgets."""

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
    """Chart view that resolves plot-area pointer positions to chart values."""

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
    """QTableWidget with stable string row IDs used by controllers."""

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
        self._action_verbs: dict[str, tuple[str, str]] = {}
        self._action_subjects: dict[str, str] = {}
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
        self._action_verbs.pop(key, None)
        self._action_subjects.pop(key, None)

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
        *,
        start_verb: str = "启动",
        stop_verb: str = "停止并平仓",
        action_subject: str = "",
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
        self._action_verbs[key] = (start_verb, stop_verb)
        self._action_subjects[key] = action_subject or key
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
        start_verb, stop_verb = self._action_verbs.get(
            key, ("启动", "停止并平仓")
        )
        verb = stop_verb if is_stop else start_verb
        subject = self._action_subjects.get(key, key)
        button.setProperty("action", "stop" if is_stop else "start")
        button.setText("●" if is_stop else "▶")
        button.setAccessibleName(f"{verb} {subject}")
        button.setToolTip(f"{verb} {subject}")
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
        self._action_verbs.clear()
        self._action_subjects.clear()

    def _write_row(
        self, row: int, values: tuple[object, ...], tags: tuple[str, ...]
    ) -> None:
        foreground = (
            QColor(self.TAG_COLORS.get(tags[0], COLORS["text"]))
            if tags
            else None
        )
        for column, value in enumerate(values):
            item = self.item(row, column)
            is_new = item is None
            if item is None:
                item = QTableWidgetItem()
            item.setText(str(value))
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter
                | (
                    Qt.AlignmentFlag.AlignLeft
                    if column == self.columnCount() - 1
                    else Qt.AlignmentFlag.AlignHCenter
                )
            )
            item.setForeground(
                foreground if foreground is not None else QColor(COLORS["text"])
            )
            if is_new:
                self.setItem(row, column, item)


__all__ = ["InteractiveChartView", "KeyedTable", "TextValue"]

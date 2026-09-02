from __future__ import annotations

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget


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


__all__ = ["ask_yes_no", "show_error", "show_info", "show_warning"]

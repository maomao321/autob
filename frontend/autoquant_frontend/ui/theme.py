from __future__ import annotations

import sys
from pathlib import Path


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


def application_icon_path() -> Path:
    """Return the icon path for both source and bundled application layouts."""
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return bundle_root / "assets" / "autoquant-icon.png"
    return (
        Path(__file__).resolve().parents[3]
        / "packaging"
        / "assets"
        / "autoquant-icon.png"
    )


def application_style_sheet() -> str:
    """Build the shared application theme independently of the main window."""
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


__all__ = ["COLORS", "application_icon_path", "application_style_sheet"]

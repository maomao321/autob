from __future__ import annotations

import threading
from datetime import datetime
from decimal import Decimal

from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QVBoxLayout

from autoquant_frontend.components.dialogs import show_error
from autoquant_frontend.components.widgets import KeyedTable
from autoquant_shared.models import TradeHistoryItem


class TradeHistoryPageMixin:
    """Persisted trade history query and presentation page."""

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

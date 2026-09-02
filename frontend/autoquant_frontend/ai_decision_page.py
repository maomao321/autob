from __future__ import annotations

import json
import threading
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
)

from autoquant_frontend.dialogs import show_error
from autoquant_frontend.widgets import KeyedTable
from autoquant_shared.models import AiDecisionHistoryItem


class AiDecisionPageMixin:
    """Persisted AI decision history and detail page."""

    def _build_ai_decision_page(self) -> None:
        layout = QVBoxLayout(self.ai_decision_page)
        layout.setContentsMargins(14, 8, 14, 14)
        layout.setSpacing(8)

        filters = QGroupBox("AI 决策记录筛选")
        filter_layout = QHBoxLayout(filters)
        filter_layout.addWidget(QLabel("标的"))
        symbol = self._line(self.ai_decision_symbol_var)
        symbol.setPlaceholderText("全部，或输入 SOXLUSDT")
        symbol.setMaximumWidth(190)
        symbol.returnPressed.connect(self._refresh_ai_decisions)
        filter_layout.addWidget(symbol)
        filter_layout.addWidget(QLabel("阶段"))
        filter_layout.addWidget(
            self._combo(
                self.ai_decision_stage_var,
                ["全部", "今日方向", "开仓时机"],
            )
        )
        filter_layout.addWidget(QLabel("最多条数"))
        limit = self._line(self.ai_decision_limit_var)
        limit.setMaximumWidth(90)
        limit.returnPressed.connect(self._refresh_ai_decisions)
        filter_layout.addWidget(limit)
        filter_layout.addStretch()
        self.ai_decision_refresh_button = self._button(
            "查询决策",
            self._refresh_ai_decisions,
            primary=True,
        )
        filter_layout.addWidget(self.ai_decision_refresh_button)
        layout.addWidget(filters)
        layout.addWidget(
            self._bound_label(
                self.ai_decision_status_var,
                muted=True,
                wrap=True,
            )
        )

        headers = [
            "决策时间",
            "标的",
            "阶段",
            "供应商",
            "模型",
            "结果",
            "置信度",
            "安全兜底",
            "总耗时",
            "响应时间",
            "结论摘要",
        ]
        widths = [155, 90, 90, 90, 155, 75, 75, 85, 75, 85, 360]
        self.ai_decision_tree = KeyedTable(
            headers,
            widths,
            multi_select=False,
        )
        self.ai_decision_tree.itemSelectionChanged.connect(
            self._show_ai_decision_detail
        )

        detail_tabs = QTabWidget()
        self.ai_decision_result_detail = QTextEdit()
        self.ai_decision_input_detail = QTextEdit()
        self.ai_decision_output_detail = QTextEdit()
        for widget in (
            self.ai_decision_result_detail,
            self.ai_decision_input_detail,
            self.ai_decision_output_detail,
        ):
            widget.setReadOnly(True)
            widget.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.ai_decision_result_detail.setPlaceholderText(
            "选择一条记录查看最终决策"
        )
        self.ai_decision_input_detail.setPlaceholderText(
            "选择一条记录查看完整 AI 输入"
        )
        self.ai_decision_output_detail.setPlaceholderText(
            "选择一条记录查看原始 API 输出"
        )
        detail_tabs.addTab(self.ai_decision_result_detail, "最终决策")
        detail_tabs.addTab(self.ai_decision_input_detail, "完整输入")
        detail_tabs.addTab(self.ai_decision_output_detail, "原始输出")

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.ai_decision_tree)
        splitter.addWidget(detail_tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([410, 260])
        layout.addWidget(splitter, 1)

    def _refresh_ai_decisions(self) -> None:
        if self._ai_decision_inflight:
            return
        try:
            limit = int(self.ai_decision_limit_var.get().strip())
            if not 1 <= limit <= 500:
                raise ValueError("最多条数必须在 1 到 500 之间")
        except ValueError as exc:
            show_error("查询条件错误", str(exc))
            return
        stage = {
            "全部": "ALL",
            "今日方向": "OPENING_DIRECTION",
            "开仓时机": "ENTRY_TIMING",
        }[self.ai_decision_stage_var.get()]
        symbol = self.ai_decision_symbol_var.get().strip().upper()
        self._ai_decision_inflight = True
        self.ai_decision_refresh_button.setEnabled(False)
        self.ai_decision_status_var.set(
            "正在查询持久化 AI 输入、输出与决策结果……"
        )

        def query() -> None:
            try:
                items = self.controller.ai_decision_history(
                    symbol=symbol,
                    stage=stage,
                    limit=limit,
                )
                self._enqueue_event(("ai_decisions", items))
            except Exception as exc:
                self._enqueue_event(("ai_decisions_error", str(exc)))

        threading.Thread(
            target=query,
            name="ai-decision-history-query",
            daemon=True,
        ).start()

    def _apply_ai_decisions(
        self, items: list[AiDecisionHistoryItem]
    ) -> None:
        self._ai_decision_inflight = False
        self.ai_decision_refresh_button.setEnabled(True)
        self.ai_decision_tree.clear_rows()
        self._ai_decisions = {item.record_id: item for item in items}
        stage_text = {
            "OPENING_DIRECTION": "今日方向",
            "ENTRY_TIMING": "开仓时机",
        }
        outcome_text = {
            "LONG": "LONG",
            "SHORT": "SHORT",
            "FLAT": "FLAT",
            "ENTER": "入场",
            "WAIT": "等待",
        }
        fallback_count = 0
        for item in items:
            if item.fallback:
                fallback_count += 1
            decided_at = datetime.fromtimestamp(
                item.decided_at / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")
            tag = "error" if item.fallback else "signal"
            self.ai_decision_tree.insert(
                "",
                None,
                iid=item.record_id,
                text=decided_at,
                values=(
                    item.symbol,
                    stage_text.get(item.stage, item.stage),
                    item.provider,
                    item.model or "-",
                    outcome_text.get(item.outcome, item.outcome),
                    f"{item.confidence:.0%}",
                    "是" if item.fallback else "否",
                    f"{item.elapsed_ms} ms",
                    f"{item.response_ms} ms",
                    item.summary,
                ),
                tags=(tag,),
            )
        self.ai_decision_status_var.set(
            f"共查询到 {len(items)} 条 AI 决策；"
            f"其中安全兜底 {fallback_count} 条"
        )
        if items:
            self.ai_decision_tree.selectRow(0)
            self._show_ai_decision_detail()
        else:
            self.ai_decision_result_detail.clear()
            self.ai_decision_input_detail.clear()
            self.ai_decision_output_detail.clear()

    def _show_ai_decision_detail(self) -> None:
        selected = self.ai_decision_tree.selection()
        if not selected:
            return
        item = self._ai_decisions.get(selected[0])
        if item is None:
            return
        factors = "\n".join(f"- {value}" for value in item.factors) or "- 无"
        risks = "\n".join(f"- {value}" for value in item.risks) or "- 无"
        self.ai_decision_result_detail.setPlainText(
            f"结果：{item.outcome}\n"
            f"置信度：{item.confidence:.2%}\n"
            f"供应商/模型：{item.provider}/{item.model or '-'}\n"
            f"安全兜底：{'是' if item.fallback else '否'}\n"
            f"总决策耗时：{item.elapsed_ms} ms\n"
            f"模型响应时间：{item.response_ms} ms\n\n"
            f"结论\n{item.summary}\n\n"
            f"主要依据\n{factors}\n\n"
            f"主要风险\n{risks}"
        )
        self.ai_decision_input_detail.setPlainText(
            self._pretty_json(item.input_json)
        )
        self.ai_decision_output_detail.setPlainText(
            self._pretty_json(item.output_json)
        )

    @staticmethod
    def _pretty_json(raw: str) -> str:
        try:
            return json.dumps(
                json.loads(raw),
                ensure_ascii=False,
                indent=2,
            )
        except (TypeError, json.JSONDecodeError):
            return raw

    def _apply_ai_decisions_error(self, message: str) -> None:
        self._ai_decision_inflight = False
        self.ai_decision_refresh_button.setEnabled(True)
        self.ai_decision_status_var.set(f"查询失败：{message}")
        show_error("AI 决策记录查询失败", message)


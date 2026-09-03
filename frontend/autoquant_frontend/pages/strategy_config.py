from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from autoquant_frontend.ui.constants import STRATEGY_LABELS
from autoquant_frontend.ui.theme import COLORS


class StrategyConfigPageMixin:
    """Strategy selection, documentation, and parameter page."""

    def _build_strategy_config_page(self) -> None:
        outer = QVBoxLayout(self.strategy_config_page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(14, 8, 14, 14)
        content_layout.setSpacing(10)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        selector = QGroupBox("运行策略")
        selector_layout = QHBoxLayout(selector)
        selector_note = QLabel(
            "每个策略使用独立标签页保存自己的参数。切换标签页即选择该运行策略；"
            "保存后对新启动的量化和新提交的回测生效。"
        )
        selector_note.setWordWrap(True)
        selector_note.setStyleSheet(f"color: {COLORS['muted']};")
        selector_layout.addWidget(selector_note)
        selector_layout.addStretch()
        selector_layout.addWidget(
            self._button("保存策略配置", self._save_config, primary=True)
        )
        content_layout.addWidget(selector)

        self.strategy_config_tabs = QTabWidget()
        self.strategy_config_tabs.setDocumentMode(True)
        self.strategy_config_pages: dict[str, QWidget] = {}

        breakout_page = QWidget()
        breakout_layout = QVBoxLayout(breakout_page)
        breakout_layout.setContentsMargins(10, 10, 10, 10)
        breakout_layout.setSpacing(10)

        description = QGroupBox("策略说明")
        description_layout = QVBoxLayout(description)
        self.five_minute_breakout_description = QLabel(
            "<b>适用周期：</b>5 分钟 K 线，固定使用 MA7 / MA25。<br><br>"
            "<b>方向来源：</b>关闭大模型时使用交易监控中的手动方向；开启大模型时"
            "使用模型给出的 LONG / SHORT / FLAT。FLAT 不产生开仓信号。<br><br>"
            "<b>做多信号：</b>MA7 在 MA25 上方；最近第一根已收盘 5 分钟 K 线"
            "收盘价高于第二根；最新价向上突破第二根最高价。<br><br>"
            "<b>做空信号：</b>MA7 在 MA25 下方；最近第一根已收盘 5 分钟 K 线"
            "收盘价低于第二根；最新价向下跌破第二根最低价。<br><br>"
            "<b>行情更新：</b>当前未收盘 K 线会随最新价实时判断；当前 K 线收盘后，"
            "第一根和第二根参考 K 线自动向前滚动。同一根当前 K 线最多发出一次信号。"
            "Futures 启动时直接加载当前时间点之前连续的 25 根已收盘 5 分钟 K 线，"
            "可跨日使用，无需等待实时 K 线补齐。"
        )
        self.five_minute_breakout_description.setObjectName(
            "fiveMinuteBreakoutDescription"
        )
        self.five_minute_breakout_description.setWordWrap(True)
        self.five_minute_breakout_description.setTextFormat(
            Qt.TextFormat.RichText
        )
        self.five_minute_breakout_description.setStyleSheet(
            f"color: {COLORS['muted']}; line-height: 1.45;"
        )
        description_layout.addWidget(self.five_minute_breakout_description)
        breakout_layout.addWidget(description)

        parameters = QGroupBox("五分钟突破 · 独立配置")
        parameters.setObjectName("fiveMinuteBreakoutSettings")
        parameter_grid = QGridLayout(parameters)
        parameter_grid.setHorizontalSpacing(12)
        parameter_grid.setVerticalSpacing(10)
        for column in (1, 3, 5):
            parameter_grid.setColumnStretch(column, 1)

        timeframe = QLineEdit("5 分钟")
        timeframe.setReadOnly(True)
        averages = QLineEdit("MA7 / MA25")
        averages.setReadOnly(True)
        self._grid_field(
            parameter_grid,
            0,
            0,
            "开仓金额(USDC/USDT)",
            self._line(self.buy_notional_var),
        )
        self._grid_field(
            parameter_grid,
            0,
            2,
            "单笔上限(USDC/USDT)",
            self._line(self.max_order_notional_var),
        )
        self._grid_field(
            parameter_grid,
            0,
            4,
            "每日开仓上限",
            self._line(self.max_daily_buy_notional_var),
        )
        self._grid_field(parameter_grid, 1, 0, "K线周期", timeframe)
        self._grid_field(parameter_grid, 1, 2, "策略均线", averages)
        self._grid_field(
            parameter_grid,
            1,
            4,
            "持仓加仓次数",
            self._line(self.max_additions_var),
        )

        risk = QWidget()
        risk_layout = QHBoxLayout(risk)
        risk_layout.setContentsMargins(0, 0, 0, 0)
        risk_layout.addWidget(self._line(self.stop_loss_var))
        risk_layout.addWidget(QLabel("/"))
        risk_layout.addWidget(self._line(self.take_profit_var))
        self._grid_field(parameter_grid, 2, 0, "止损/止盈(%)", risk)
        self._grid_field(
            parameter_grid,
            2,
            2,
            "信号有效期(秒)",
            self._line(self.max_signal_age_var),
        )
        self._grid_field(
            parameter_grid,
            2,
            4,
            "AI时机K线数量",
            self._line(self.ai_entry_timing_bars_var),
        )
        parameter_note = QLabel(
            "加仓次数只统计本次持仓期间的同向加仓；仓位完全平掉后重新计数。"
            "止损、止盈或策略退出会平掉程序记录的全部当前持仓。"
            "AI 时机 K 线数量仅在启用大模型决策时使用。"
        )
        parameter_note.setWordWrap(True)
        parameter_note.setStyleSheet(f"color: {COLORS['muted']};")
        parameter_grid.addWidget(parameter_note, 3, 0, 1, 6)
        breakout_layout.addWidget(parameters)
        breakout_layout.addStretch()

        self.strategy_config_pages["five_minute_breakout"] = breakout_page
        self.strategy_config_tabs.addTab(
            breakout_page, STRATEGY_LABELS["five_minute_breakout"]
        )
        content_layout.addWidget(self.strategy_config_tabs)

        self.strategy_config_tabs.currentChanged.connect(
            self._select_strategy_from_tab
        )
        self._select_strategy_config(self.strategy_var.get())

    def _select_strategy_config(self, strategy: str) -> None:
        page = self.strategy_config_pages.get(str(strategy))
        if page is not None:
            self.strategy_config_tabs.setCurrentWidget(page)

    def _select_strategy_from_tab(self, index: int) -> None:
        page = self.strategy_config_tabs.widget(index)
        for strategy, strategy_page in self.strategy_config_pages.items():
            if page is strategy_page:
                self.strategy_var.set(strategy)
                return

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from autoquant_frontend.theme import COLORS


class ConfigPageMixin:
    """Runtime and AI provider configuration page."""

    def _build_config_page(self) -> None:
        outer = QVBoxLayout(self.config_page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(14, 8, 14, 14)
        content_layout.setSpacing(10)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        settings = QGroupBox("运行配置")
        settings.setObjectName("runtimeSettingsGroup")
        grid = QGridLayout(settings)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        for column in (1, 3, 5, 7):
            grid.setColumnStretch(column, 1)

        self._grid_field(
            grid,
            0,
            0,
            "API 供应商",
            self._combo(self.provider_var, ["binance_stocks", "binance_futures"]),
        )
        self._grid_field(grid, 0, 2, "交易模式", self._combo(self.mode_var, ["PAPER", "REAL"]))
        self._grid_field(grid, 0, 4, "Futures 杠杆倍数", self._line(self.leverage_var))
        save_button = self._button("保存配置", self._save_config, primary=True)
        grid.addWidget(save_button, 0, 6, 1, 2)

        self._grid_field(grid, 1, 0, "API Key", self._line(self.api_key_var), span=2)
        self._grid_field(grid, 1, 3, "API Secret", self._line(self.api_secret_var, secret=True), span=2)
        grid.addWidget(self._button("检查 API 与标的", self._check_connection), 1, 6, 1, 2)

        warning = QLabel(
            "默认 PAPER 只记录模拟订单。REAL 会真实下单；Stocks 只支持做多，"
            "Futures 支持做多和做空，但仅支持单向持仓；持仓期间可按策略配置"
            "同向加仓，仍禁止反向开仓。"
            "Futures 实盘下单前设置所选杠杆。“停止并平仓”会减掉全部程序持仓；"
            "未知订单会锁定实盘。"
            "API Key/Secret 会保存到后端服务器，请保护服务器配置和访问令牌。"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {COLORS['warning']};")
        grid.addWidget(warning, 2, 0, 1, 8)
        content_layout.addWidget(settings)

        ai_settings = QGroupBox("大模型开仓决策")
        ai_layout = QVBoxLayout(ai_settings)
        ai_layout.setSpacing(12)

        ai_header = QHBoxLayout()
        self.ai_enabled_checkbox = QCheckBox(
            "启用大模型决策（今日方向 + 候选开仓时机）"
        )
        self.ai_enabled_checkbox.setChecked(
            self.config.ai_provider != "DISABLED"
        )
        ai_header.addWidget(self.ai_enabled_checkbox)
        ai_header.addStretch()
        ai_header.addWidget(QLabel("模型模式"))
        self.ai_provider_combo = self._combo(
            self.ai_provider_var,
            ["CHATGPT", "DEEPSEEK", "QWEN", "DUAL"],
        )
        self.ai_provider_combo.setMinimumWidth(180)
        ai_header.addWidget(self.ai_provider_combo)
        ai_layout.addLayout(ai_header)

        provider_layout = QHBoxLayout()
        provider_layout.setSpacing(12)

        self.openai_settings_group = QGroupBox("OpenAI")
        openai_grid = QGridLayout(self.openai_settings_group)
        openai_grid.setColumnStretch(1, 1)
        self._grid_field(
            openai_grid,
            0,
            0,
            "API Key",
            self._line(self.openai_api_key_var, secret=True),
        )
        self._grid_field(
            openai_grid,
            1,
            0,
            "模型",
            self._line(self.openai_model_var),
        )
        self._grid_field(
            openai_grid,
            2,
            0,
            "推理设置",
            self._openai_reasoning_control(),
        )
        openai_note = QLabel(
            "GPT-5/o 系列可指定 reasoning.effort；关闭时不发送推理参数。"
        )
        openai_note.setWordWrap(True)
        openai_note.setStyleSheet(f"color: {COLORS['muted']};")
        openai_grid.addWidget(openai_note, 3, 0, 1, 2)
        provider_layout.addWidget(self.openai_settings_group, 1)

        self.deepseek_settings_group = QGroupBox("DeepSeek")
        deepseek_grid = QGridLayout(self.deepseek_settings_group)
        deepseek_grid.setColumnStretch(1, 1)
        self._grid_field(
            deepseek_grid,
            0,
            0,
            "API Key",
            self._line(self.deepseek_api_key_var, secret=True),
        )
        self._grid_field(
            deepseek_grid,
            1,
            0,
            "模型",
            self._line(self.deepseek_model_var),
        )
        self._grid_field(
            deepseek_grid,
            2,
            0,
            "推理设置",
            self._deepseek_reasoning_control(),
        )
        deepseek_note = QLabel(
            "V4 模型支持思考开关；强度 low / high / max。"
        )
        deepseek_note.setWordWrap(True)
        deepseek_note.setStyleSheet(f"color: {COLORS['muted']};")
        deepseek_grid.addWidget(deepseek_note, 3, 0, 1, 2)
        provider_layout.addWidget(self.deepseek_settings_group, 1)

        self.qwen_settings_group = QGroupBox("Qwen · 阿里云百炼")
        qwen_grid = QGridLayout(self.qwen_settings_group)
        qwen_grid.setColumnStretch(1, 1)
        self._grid_field(
            qwen_grid,
            0,
            0,
            "API Key",
            self._line(self.qwen_api_key_var, secret=True),
        )
        self._grid_field(
            qwen_grid,
            1,
            0,
            "模型",
            self._line(self.qwen_model_var),
        )
        self._grid_field(
            qwen_grid,
            2,
            0,
            "Chat 接口",
            self._line(self.qwen_chat_url_var),
        )
        self._grid_field(
            qwen_grid,
            3,
            0,
            "推理设置",
            self._qwen_reasoning_control(),
        )
        qwen_note = QLabel(
            "Qwen3+ 支持思考模式；具体强度档位随模型变化。"
        )
        qwen_note.setWordWrap(True)
        qwen_note.setStyleSheet(f"color: {COLORS['muted']};")
        qwen_grid.addWidget(qwen_note, 4, 0, 1, 2)
        provider_layout.addWidget(self.qwen_settings_group, 1)
        ai_layout.addLayout(provider_layout)

        self.ai_common_group = QGroupBox("通用决策参数")
        ai_grid = QGridLayout(self.ai_common_group)
        ai_grid.setHorizontalSpacing(12)
        ai_grid.setVerticalSpacing(10)
        ai_grid.setColumnStretch(1, 1)
        ai_grid.setColumnStretch(3, 1)
        self._grid_field(
            ai_grid,
            0,
            0,
            "最低置信度",
            self._line(self.ai_min_confidence_var),
        )
        self._grid_field(
            ai_grid,
            0,
            2,
            "决策超时(秒，最高600)",
            self._line(self.ai_timeout_var),
        )
        ai_history_line = self._line(self.ai_history_days_var)
        ai_history_line.setEnabled(False)
        self._grid_field(
            ai_grid,
            1,
            0,
            "方向日线(固定30根)",
            ai_history_line,
        )
        news_window = QWidget()
        news_window_layout = QHBoxLayout(news_window)
        news_window_layout.setContentsMargins(0, 0, 0, 0)
        news_window_layout.addWidget(self._line(self.ai_news_days_var))
        news_window_layout.addWidget(QLabel("/"))
        news_window_layout.addWidget(self._line(self.ai_news_limit_var))
        self._grid_field(
            ai_grid,
            1,
            2,
            "新闻天数/条数",
            news_window,
        )
        ai_layout.addWidget(self.ai_common_group)

        ai_note = QLabel(
            "开关关闭时完全使用表格中的手动方向，不调用大模型。"
            "开关开启时，模型先生成今日 LONG/SHORT/FLAT，"
            "方向判断使用最近30根日线OHLC；再使用今日日线和配置数量的"
            "五分钟K线OHLC判断每个候选信号的 ENTER/WAIT。"
            "失败、低置信度或双模型分歧时不开仓。"
            "Qwen 使用阿里云百炼 OpenAI 兼容 Chat 接口；新工作区可填写专属接口地址。"
            "大模型配置和 API Key 会保存到后端本地配置文件；"
            "OpenAI Key 也可用于交易经验上传。"
        )
        ai_note.setWordWrap(True)
        ai_note.setStyleSheet(f"color: {COLORS['muted']};")
        ai_layout.addWidget(ai_note)

        self.ai_provider_combo.currentTextChanged.connect(
            self._update_ai_provider_layout
        )
        self.ai_enabled_checkbox.toggled.connect(
            self._update_ai_controls_enabled
        )
        self._update_ai_provider_layout(self.ai_provider_var.get())
        self._update_ai_controls_enabled(
            self.ai_enabled_checkbox.isChecked()
        )
        content_layout.addWidget(ai_settings)
        content_layout.addStretch()

    def _reasoning_control(
        self,
        *,
        provider: str,
        enabled: bool,
        effort_var: TextValue,
        effort_values: list[str],
        tooltip: str,
    ) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        checkbox = QCheckBox("启用")
        checkbox.setChecked(enabled)
        checkbox.setToolTip(tooltip)
        effort = self._combo(effort_var, effort_values)
        effort.setMinimumWidth(110)
        effort.setToolTip(tooltip)
        effort.setEnabled(enabled)
        checkbox.toggled.connect(effort.setEnabled)
        layout.addWidget(checkbox)
        layout.addWidget(QLabel("强度"))
        layout.addWidget(effort)
        layout.addStretch()
        setattr(self, f"{provider}_reasoning_checkbox", checkbox)
        setattr(self, f"{provider}_reasoning_effort_combo", effort)
        return container

    def _openai_reasoning_control(self) -> QWidget:
        return self._reasoning_control(
            provider="openai",
            enabled=self.config.openai_reasoning_enabled,
            effort_var=self.openai_reasoning_effort_var,
            effort_values=["low", "medium", "high", "xhigh", "max"],
            tooltip=(
                "启用后向 OpenAI Responses API 发送 reasoning.effort；"
                "关闭时不发送该参数，以兼容非推理模型。"
            ),
        )

    def _deepseek_reasoning_control(self) -> QWidget:
        control = self._reasoning_control(
            provider="deepseek",
            enabled=self.config.deepseek_thinking_enabled,
            effort_var=self.deepseek_reasoning_effort_var,
            effort_values=["low", "medium", "high", "max"],
            tooltip=(
                "启用后发送 thinking=enabled 和 reasoning_effort；"
                "DeepSeek V4 会把 medium 映射为 high。"
            ),
        )
        self.deepseek_thinking_checkbox = self.deepseek_reasoning_checkbox
        return control

    def _qwen_reasoning_control(self) -> QWidget:
        return self._reasoning_control(
            provider="qwen",
            enabled=self.config.qwen_thinking_enabled,
            effort_var=self.qwen_reasoning_effort_var,
            effort_values=["low", "medium", "high", "xhigh", "max"],
            tooltip=(
                "启用后向百炼 Chat 接口发送 enable_thinking=true 和 "
                "reasoning_effort；支持档位由具体 Qwen 模型决定。"
            ),
        )

    def _update_ai_provider_layout(self, mode: str = "") -> None:
        selected = (mode or self.ai_provider_var.get()).strip().upper()
        self.openai_settings_group.setVisible(
            selected in {"CHATGPT", "DUAL"}
        )
        self.deepseek_settings_group.setVisible(
            selected in {"DEEPSEEK", "DUAL"}
        )
        self.qwen_settings_group.setVisible(selected == "QWEN")

    def _update_ai_controls_enabled(self, enabled: bool) -> None:
        self.openai_settings_group.setEnabled(enabled)
        self.deepseek_settings_group.setEnabled(enabled)
        self.qwen_settings_group.setEnabled(enabled)
        self.ai_common_group.setEnabled(enabled)

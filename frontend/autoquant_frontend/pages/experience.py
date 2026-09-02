from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from autoquant_frontend.components.dialogs import ask_yes_no, show_error, show_info
from autoquant_frontend.services.experience import (
    ExperienceError,
    ExperienceImportResult,
    OpenAIVectorStoreUploader,
    UploadResult,
    default_experience_path,
    import_external_experiences,
    merge_experience_document,
    summarize_experiences,
    write_experience_document,
)
from autoquant_frontend.ui.theme import COLORS
from autoquant_frontend.components.widgets import KeyedTable


class ExperiencePageMixin:
    """Experience import, persistence, export, and upload page."""

    def _build_experience_page(self) -> None:
        layout = QVBoxLayout(self.experience_page)
        layout.setContentsMargins(14, 8, 14, 14)
        layout.setSpacing(8)
        source = QGroupBox("1. 导入外部交易经验")
        source_grid = QGridLayout(source)
        source_grid.setColumnStretch(1, 1)
        source_grid.addWidget(QLabel("交易记录 Excel/CSV（可选）"), 0, 0)
        source_grid.addWidget(self._line(self.experience_trade_path_var), 0, 1)
        source_grid.addWidget(self._button("选择文件", self._browse_experience_trade_file), 0, 2)
        source_grid.addWidget(QLabel("K线形态 Excel/CSV（可选）"), 1, 0)
        source_grid.addWidget(self._line(self.experience_kline_path_var), 1, 1)
        source_grid.addWidget(self._button("选择文件", self._browse_experience_kline_file), 1, 2)
        pattern_row = QHBoxLayout()
        pattern_row.addWidget(QLabel("每个形态最多K线数"))
        bars = self._line(self.experience_pattern_bars_var)
        bars.setMaximumWidth(100)
        pattern_row.addWidget(bars)
        pattern_row.addStretch()
        self.experience_extract_button = self._button("导入并预览", self._extract_experience_records, primary=True)
        pattern_row.addWidget(self.experience_extract_button)
        source_grid.addLayout(pattern_row, 2, 0, 1, 3)
        source_note = QLabel(
            "两类文件均可单独导入，也可用相同 trade_id / pattern_id 关联。交易记录需包含标的、开平仓时间/价格和数量；"
            "K线需包含时间与 OHLC。不会读取本程序订单账本，且关联交易时只采用开仓前已收盘K线。"
        )
        source_note.setWordWrap(True)
        source_note.setStyleSheet(f"color: {COLORS['muted']};")
        source_grid.addWidget(source_note, 3, 0, 1, 3)
        layout.addWidget(source)

        summary_row = QHBoxLayout()
        summary = self._bound_label(self.experience_summary_var)
        summary_font = summary.font()
        summary_font.setPointSize(12)
        summary_font.setWeight(QFont.Weight.Bold)
        summary.setFont(summary_font)
        summary_row.addWidget(summary)
        summary_row.addStretch()
        bias_note = QLabel("盈利和亏损样本会一起上传，避免幸存者偏差。")
        bias_note.setStyleSheet(f"color: {COLORS['warning']};")
        summary_row.addWidget(bias_note)
        layout.addLayout(summary_row)

        headers = ["股票", "结果", "来源", "开始时间(UTC)", "数量", "入场/起始价", "出场/结束价", "净盈亏", "收益率(%)", "持有时间", "K线形态"]
        widths = [75, 65, 75, 165, 80, 90, 90, 85, 85, 90, 210]
        self.experience_tree = KeyedTable(headers, widths, multi_select=False)
        layout.addWidget(self.experience_tree, 1)

        upload = QGroupBox("2. 保存或上传知识库")
        upload_grid = QGridLayout(upload)
        upload_grid.setColumnStretch(1, 1)
        upload_grid.addWidget(QLabel("本地共享经验库"), 0, 0)
        path_label = QLabel(str(default_experience_path()))
        path_label.setStyleSheet(f"color: {COLORS['muted']};")
        upload_grid.addWidget(path_label, 0, 1)
        upload_grid.addWidget(self._button("保存到本地", self._save_local_experience_library), 0, 2)
        upload_grid.addWidget(self._button("另存为 JSON", self._export_experience_records), 0, 3)
        upload_grid.addWidget(QLabel("OpenAI Vector Store ID"), 1, 0)
        upload_grid.addWidget(self._line(self.experience_vector_store_var), 1, 1)
        self.experience_upload_button = self._button("上传到 OpenAI", self._upload_experience_records, primary=True)
        upload_grid.addWidget(self.experience_upload_button, 1, 2, 1, 2)
        upload_note = QLabel("ID 留空会创建新的 Vector Store；已有 ID 则追加文件。DeepSeek 暂无本页托管上传目标，后续由本地共享经验库检索后提供给它。")
        upload_note.setWordWrap(True)
        upload_note.setStyleSheet(f"color: {COLORS['muted']};")
        upload_grid.addWidget(upload_note, 2, 0, 1, 4)
        upload_grid.addWidget(self._bound_label(self.experience_status_var, color=COLORS["signal"], wrap=True), 3, 0, 1, 4)
        layout.addWidget(upload)

    @staticmethod
    def _ask_experience_file(title: str) -> str:
        selected, _ = QFileDialog.getOpenFileName(
            _message_parent(), title, "", "Excel 或 CSV (*.xlsx *.csv);;Excel 工作簿 (*.xlsx);;CSV 文件 (*.csv);;所有文件 (*)"
        )
        return selected

    def _browse_experience_trade_file(self) -> None:
        selected = self._ask_experience_file("选择外部交易记录")
        if selected:
            self.experience_trade_path_var.set(selected)

    def _browse_experience_kline_file(self) -> None:
        selected = self._ask_experience_file("选择外部K线形态")
        if selected:
            self.experience_kline_path_var.set(selected)

    def _extract_experience_records(self) -> None:
        if self._experience_extract_inflight:
            return
        try:
            pattern_bars = int(self.experience_pattern_bars_var.get())
            if not 5 <= pattern_bars <= 240:
                raise ValueError("每个形态的K线数量必须在 5 到 240 之间")
            trade_text = self.experience_trade_path_var.get().strip()
            kline_text = self.experience_kline_path_var.get().strip()
            trade_path = Path(trade_text) if trade_text else None
            kline_path = Path(kline_text) if kline_text else None
            if trade_path is None and kline_path is None:
                raise ValueError("请至少选择一个交易记录或K线形态文件")
            for label, path in (("交易记录", trade_path), ("K线形态", kline_path)):
                if path is None:
                    continue
                if not path.is_file():
                    raise ValueError(f"选择的{label}文件不存在")
                if path.suffix.lower() not in {".xlsx", ".csv"}:
                    raise ValueError(f"{label}只支持 .xlsx 或 .csv 文件")
        except ValueError as exc:
            show_error("导入配置错误", str(exc))
            return

        self._experience_extract_inflight = True
        self.experience_extract_button.setEnabled(False)
        self.experience_status_var.set("正在读取外部交易记录和K线形态……")

        def extract() -> None:
            try:
                result = import_external_experiences(
                    trade_path=trade_path, kline_path=kline_path, pattern_bars=pattern_bars
                )
                self._enqueue_event(("experience_extracted", result))
            except Exception as exc:
                self._enqueue_event(("experience_error", "extract", str(exc) or exc.__class__.__name__))

        threading.Thread(target=extract, name="experience-extractor", daemon=True).start()

    def _apply_experience_records(self, result: ExperienceImportResult) -> None:
        self._experience_extract_inflight = False
        self.experience_extract_button.setEnabled(True)
        experiences = result.experiences
        self._experiences = experiences
        self.experience_tree.clear_rows()
        outcome_text = {"WIN": "盈利", "LOSS": "亏损", "BREAKEVEN": "持平", "UNLABELED": "形态"}
        for index, item in enumerate(experiences):
            pattern = item.pre_entry_pattern
            kline = (
                f"{pattern.get('bar_count', 0)}根 / {pattern.get('shape_signature', '未分类')}"
                if pattern.get("available") else "未提供"
            )
            tag = "win" if item.outcome == "WIN" else "loss" if item.outcome == "LOSS" else ""
            self.experience_tree.insert(
                "", None, iid=f"experience-{index}", text=item.symbol,
                values=(
                    outcome_text.get(item.outcome, item.outcome),
                    "交易记录" if item.record_type == "TRADE" else "K线形态",
                    item.entry_time.replace("+00:00", "Z"), item.quantity,
                    item.entry_price, item.exit_price, item.net_pnl,
                    item.return_percent, self._format_holding_seconds(item.holding_seconds), kline,
                ),
                tags=(tag,) if tag else (),
            )
        summary = summarize_experiences(experiences)
        self.experience_summary_var.set(
            f"经验 {summary.total} 条｜交易 {summary.trades}｜形态 {summary.patterns}｜"
            f"盈利 {summary.wins}｜亏损 {summary.losses}｜持平 {summary.breakeven}｜"
            f"含K线 {summary.with_kline}｜交易净盈亏 {summary.net_pnl:.2f}"
        )
        if experiences:
            detail = f"已导入 {result.trade_rows} 条交易记录"
            if result.kline_rows:
                detail += f"和 {result.kline_rows} 根K线"
            self.experience_status_var.set(detail + "；请核对后保存或上传。")
        else:
            self.experience_status_var.set("文件已读取，但没有可导入的交易记录或K线形态。")

    def _save_local_experience_library(self) -> None:
        if not self._experiences:
            show_info("没有经验", "请先从外部文件导入至少一条经验。")
            return
        try:
            path, added, total = merge_experience_document(default_experience_path(), self._experiences)
            self.experience_status_var.set(f"已保存本地经验库：新增 {added} 笔，共 {total} 笔；{path}")
            show_info("保存完成", f"本地经验库共 {total} 笔：\n{path}")
        except ExperienceError as exc:
            show_error("保存失败", str(exc))

    def _export_experience_records(self) -> None:
        if not self._experiences:
            show_info("没有经验", "请先从外部文件导入至少一条经验。")
            return
        selected, _ = QFileDialog.getSaveFileName(
            self, "导出交易经验", "external_trade_experiences.json", "JSON 文件 (*.json)"
        )
        if not selected:
            return
        if not selected.lower().endswith(".json"):
            selected += ".json"
        try:
            path = write_experience_document(Path(selected), self._experiences)
            self.experience_status_var.set(f"已导出 {len(self._experiences)} 笔：{path}")
        except ExperienceError as exc:
            show_error("导出失败", str(exc))

    def _upload_experience_records(self) -> None:
        if self._experience_upload_inflight:
            return
        if not self._experiences:
            show_info("没有经验", "请先从外部文件导入至少一条经验。")
            return
        api_key = self.openai_api_key_var.get().strip()
        if not api_key or api_key == SECRET_SENTINEL:
            show_error(
                "缺少凭据",
                "请先在“运行配置”页重新填写真实 OpenAI API Key。",
            )
            return
        vector_store_id = self.experience_vector_store_var.get().strip()
        confirmed = ask_yes_no(
            "确认上传交易数据",
            "将把本次外部数据合并到本地经验库，并把合并后的股票代码、成交价格、"
            "盈亏、持有时间和K线形态上传到 OpenAI Vector Store。API Key 不会写入文件。\n\n"
            f"本次导入记录：{len(self._experiences)} 条。确认继续吗？",
        )
        if not confirmed:
            return
        try:
            timeout_seconds = max(5, min(120, int(self.ai_timeout_var.get())))
        except ValueError:
            timeout_seconds = 30
        self._experience_upload_inflight = True
        self.experience_upload_button.setEnabled(False)
        self.experience_status_var.set("正在保存本地经验库并上传到 OpenAI……")

        def upload() -> None:
            try:
                path, added, total = merge_experience_document(default_experience_path(), self._experiences)
                result = OpenAIVectorStoreUploader().upload(
                    path, api_key=api_key, vector_store_id=vector_store_id,
                    timeout_seconds=timeout_seconds,
                )
                self._enqueue_event(("experience_uploaded", result, added, total, str(path)))
            except Exception as exc:
                self._enqueue_event(("experience_error", "upload", str(exc) or exc.__class__.__name__))

        threading.Thread(target=upload, name="experience-uploader", daemon=True).start()

    def _apply_experience_upload(self, result: UploadResult, added: int, total: int, path: str) -> None:
        self._experience_upload_inflight = False
        self.experience_upload_button.setEnabled(True)
        self.experience_vector_store_var.set(result.vector_store_id)
        self.experience_status_var.set(
            f"上传已受理：Vector Store {result.vector_store_id}，文件 {result.file_id}，索引状态 {result.status}。"
        )
        show_info(
            "上传已受理",
            f"本地库新增 {added} 笔，共 {total} 笔：\n{path}\n\nVector Store：{result.vector_store_id}\n"
            f"文件：{result.file_id}\n状态：{result.status}",
        )

    def _apply_experience_error(self, operation: str, message: str) -> None:
        if operation == "extract":
            self._experience_extract_inflight = False
            self.experience_extract_button.setEnabled(True)
            title = "经验导入失败"
        else:
            self._experience_upload_inflight = False
            self.experience_upload_button.setEnabled(True)
            title = "经验上传失败"
        self.experience_status_var.set(message)
        show_error(title, message)

    @staticmethod
    def _format_holding_seconds(seconds: int) -> str:
        hours, remainder = divmod(max(0, seconds), 3600)
        minutes, remaining_seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}时{minutes}分"
        if minutes:
            return f"{minutes}分{remaining_seconds}秒"
        return f"{remaining_seconds}秒"

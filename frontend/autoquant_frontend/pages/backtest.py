from __future__ import annotations

import re
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtCharts import (
    QChart,
    QChartView,
    QLineSeries,
    QScatterSeries,
    QValueAxis,
)
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from autoquant_frontend.services.client import BacktestStatusListener
from autoquant_frontend.ui.constants import (
    BACKTEST_DOWNLOAD_ACTION_COLUMN,
    STRATEGY_LABELS,
    STRATEGY_OPTIONS,
)
from autoquant_frontend.components.dialogs import ask_yes_no, show_error, show_info
from autoquant_frontend.ui.theme import COLORS
from autoquant_frontend.components.widgets import InteractiveChartView, KeyedTable
from autoquant_shared.config import strategy_config_snapshot


class BacktestPageMixin:
    """Historical data and strategy backtest page orchestration."""

    def _start_backtest_status_listener(self) -> None:
        if self._closed or self._backtest_status_listener is not None:
            return
        listener = BacktestStatusListener(
            self.backend_client,
            status_callback=lambda downloads, runs: self._enqueue_event(
                ("backtest_status", downloads, runs, "")
            ),
            error_callback=lambda error: self._enqueue_event(
                ("backtest_status", [], [], error)
            ),
        )
        self._backtest_status_listener = listener
        listener.start()

    def _stop_backtest_status_listener(self) -> None:
        listener = self._backtest_status_listener
        self._backtest_status_listener = None
        if listener is not None:
            listener.close()

    def _build_backtest_page(self) -> None:
        layout = QVBoxLayout(self.backtest_page)
        layout.setContentsMargins(14, 8, 14, 14)
        layout.setSpacing(8)

        controls = QGroupBox("策略回测")
        controls_layout = QHBoxLayout(controls)
        controls_layout.addWidget(QLabel("标的"))
        symbol = self._line(self.backtest_symbol_var)
        symbol.setPlaceholderText("例如 BTCUSDT")
        symbol.setMaximumWidth(180)
        controls_layout.addWidget(symbol)
        self.backtest_download_button = self._button(
            "下载", self._start_backtest_download, primary=True
        )
        self.backtest_import_button = self._button(
            "导入 K 线", self._import_historical_bars
        )
        self.backtest_refresh_button = self._button(
            "刷新", lambda: self._refresh_backtest_data(manual=True)
        )
        controls_layout.addWidget(self.backtest_download_button)
        controls_layout.addWidget(self.backtest_import_button)
        controls_layout.addWidget(self.backtest_refresh_button)
        controls_layout.addStretch()
        layout.addWidget(controls)

        note = QLabel(
            "使用当前运行配置中的行情源、开仓金额、持仓加仓次数及止盈止损参数，"
            "策略均线固定为 MA7/MA25。"
            "最多回看 180 天，标的历史不足时以行情源实际返回数量为准；"
            "收益率 = 总盈亏 ÷ 单笔开仓金额，最大回撤按单笔开仓金额作为初始资金计算；"
            "金额和百分比统一显示两位小数。分页下载目前支持 Binance Futures，"
            "数据和回测结果均保存在后端 SQLite。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {COLORS['muted']};")
        layout.addWidget(note)

        status = QLabel()
        self.backtest_status_var.bind_label(status)
        status.setWordWrap(True)
        status.setStyleSheet(f"color: {COLORS['muted']};")
        layout.addWidget(status)

        downloads_box = QGroupBox("历史 K 线")
        downloads_layout = QVBoxLayout(downloads_box)
        self.backtest_download_tree = KeyedTable(
            [
                "标的", "回测", "创建时间", "更新时间", "行情源", "状态",
                "进度", "日线", "5分钟", "1分钟", "说明",
            ],
            [100, 72, 150, 150, 125, 80, 70, 65, 75, 80, 260],
            multi_select=False,
        )
        self.backtest_download_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.backtest_download_tree.customContextMenuRequested.connect(
            self._show_backtest_download_context_menu
        )
        downloads_layout.addWidget(self.backtest_download_tree)

        runs_box = QGroupBox("回测结果")
        runs_layout = QVBoxLayout(runs_box)
        self.backtest_run_tree = KeyedTable(
            [
                "完成时间", "标的", "策略", "状态", "交易数", "胜/负",
                "总盈亏", "收益率", "最大回撤", "回测明细", "说明",
            ],
            [150, 100, 170, 80, 65, 70, 105, 85, 90, 90, 220],
            multi_select=False,
        )
        runs_layout.addWidget(self.backtest_run_tree)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(downloads_box)
        splitter.addWidget(runs_box)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([310, 310])
        layout.addWidget(splitter, 1)

    def _backtest_symbol(self) -> str:
        symbol = self.backtest_symbol_var.get().strip().upper()
        if not symbol:
            raise ValueError("请输入回测标的")
        if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,19}", symbol) is None:
            raise ValueError("回测标的格式不正确")
        return symbol

    def _set_backtest_actions_enabled(self, enabled: bool) -> None:
        self.backtest_download_button.setEnabled(enabled)
        self.backtest_import_button.setEnabled(enabled)

    def _start_backtest_download(self) -> None:
        if self._backtest_action_inflight:
            return
        try:
            symbol = self._backtest_symbol()
        except ValueError as exc:
            show_error("无法下载", str(exc))
            return
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_status_var.set(f"正在提交 {symbol} 的 180 天历史 K 线下载任务…")

        def submit() -> None:
            try:
                job_id = self.backend_client.start_historical_download(symbol)
                self._enqueue_event(("backtest_action", "download", job_id, ""))
            except Exception as exc:
                self._enqueue_event(("backtest_action", "download", "", str(exc)))

        threading.Thread(
            target=submit, name="backtest-download-submit", daemon=True
        ).start()

    def _start_backtest_run(self) -> None:
        if self._backtest_action_inflight:
            return
        try:
            symbol = self._backtest_symbol()
            strategy = self.backtest_strategy_var.get().strip()
            if not strategy:
                raise ValueError("请选择回测策略")
        except ValueError as exc:
            show_error("无法回测", str(exc))
            return
        self._submit_backtest_run(symbol, strategy)

    def _choose_backtest_strategy(self, symbol: str) -> str | None:
        strategies = list(STRATEGY_OPTIONS)
        labels = [STRATEGY_LABELS.get(item, item) for item in strategies]
        current = self.backtest_strategy_var.get().strip()
        current_index = strategies.index(current) if current in strategies else 0
        selected, accepted = QInputDialog.getItem(
            self,
            "选择回测策略",
            f"{symbol} 回测策略",
            labels,
            current_index,
            False,
        )
        if not accepted:
            return None
        strategy = strategies[labels.index(selected)]
        self.backtest_strategy_var.set(strategy)
        return strategy

    def _start_download_backtest(self, download_id: str) -> None:
        if self._backtest_action_inflight:
            return
        item = self._backtest_downloads.get(download_id)
        if item is None:
            return
        symbol = str(item.get("symbol", "")).strip().upper()
        provider = str(item.get("provider", "")).strip().lower()
        strategy = self._choose_backtest_strategy(symbol)
        if strategy is None:
            return
        self._submit_backtest_run(
            symbol,
            strategy,
            download_id=download_id,
            provider=provider,
        )

    def _submit_backtest_run(
        self,
        symbol: str,
        strategy: str,
        *,
        download_id: str = "",
        provider: str = "",
    ) -> None:
        try:
            current_strategy_config = strategy_config_snapshot(
                self._current_config(), strategy
            )
        except ValueError as exc:
            show_error("无法回测", str(exc))
            return
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_status_var.set(f"正在提交 {symbol} 的策略回测任务…")
        if download_id:
            self.backtest_download_tree.set_action_state(
                download_id, action="start", enabled=False
            )

        def submit() -> None:
            try:
                run_id = self.backend_client.start_backtest(
                    symbol,
                    strategy,
                    {
                        key: value
                        for key, value in current_strategy_config.items()
                        if key
                        not in {
                            "strategy",
                            "strategy_name",
                            "kline_interval",
                            "fast_ma_period",
                            "slow_ma_period",
                        }
                    },
                    download_id=download_id,
                    provider=provider,
                )
                self._enqueue_event(
                    ("backtest_action", "run", run_id, "", download_id)
                )
            except Exception as exc:
                self._enqueue_event(
                    ("backtest_action", "run", "", str(exc), download_id)
                )

        threading.Thread(
            target=submit, name="backtest-run-submit", daemon=True
        ).start()

    def _stop_download_backtest(self, download_id: str) -> None:
        if self._backtest_action_inflight:
            return
        run = self._backtest_active_runs.get(download_id)
        if run is None:
            return
        run_id = str(run.get("run_id", ""))
        symbol = str(run.get("symbol", ""))
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_download_tree.set_action_state(
            download_id, action="stop", enabled=False
        )
        self.backtest_status_var.set(f"正在停止 {symbol} 的回测任务…")

        def stop() -> None:
            try:
                self.backend_client.stop_backtest(run_id)
                self._enqueue_event(
                    ("backtest_stop", run_id, download_id, "")
                )
            except Exception as exc:
                self._enqueue_event(
                    ("backtest_stop", run_id, download_id, str(exc))
                )

        threading.Thread(
            target=stop, name="backtest-run-stop", daemon=True
        ).start()

    def _export_historical_bars(self) -> None:
        if self._backtest_action_inflight:
            return
        try:
            symbol = self._backtest_symbol()
        except ValueError as exc:
            show_error("无法导出", str(exc))
            return
        self._begin_historical_export(symbol, "")

    def _show_backtest_download_context_menu(self, position: QPoint) -> None:
        index = self.backtest_download_tree.indexAt(position)
        if not index.isValid():
            return
        self.backtest_download_tree.selectRow(index.row())
        selected = self.backtest_download_tree.selection()
        if not selected:
            return
        item = self._backtest_downloads.get(selected[0])
        if item is None:
            return
        symbol = str(item.get("symbol", "")).strip().upper()
        provider = str(item.get("provider", "")).strip().lower()
        has_active_download = any(
            str(candidate.get("symbol", "")).strip().upper() == symbol
            and str(candidate.get("provider", "")).strip().lower() == provider
            and str(candidate.get("status", "")) in {"QUEUED", "RUNNING"}
            for candidate in self._backtest_downloads.values()
        )
        menu = QMenu(self.backtest_download_tree)
        update_action = menu.addAction("更新 K 线到最新")
        export_action = menu.addAction("导出该标的 K 线")
        delete_action = menu.addAction("删除该标的 K 线")
        export_action.setEnabled(
            sum(
                int(item.get(field, 0) or 0)
                for field in (
                    "daily_count",
                    "five_minute_count",
                    "one_minute_count",
                )
            )
            > 0
        )
        delete_action.setEnabled(not has_active_download)
        update_action.setEnabled(not has_active_download)
        update_action.triggered.connect(
            lambda _checked=False: self._update_historical_bars(
                str(item.get("download_id", "")), symbol
            )
        )
        export_action.triggered.connect(
            lambda _checked=False: self._begin_historical_export(
                symbol, provider
            )
        )
        delete_action.triggered.connect(
            lambda _checked=False: self._delete_historical_bars(
                symbol, provider, item
            )
        )
        menu.exec(
            self.backtest_download_tree.viewport().mapToGlobal(position)
        )

    def _update_historical_bars(
        self, download_id: str, symbol: str
    ) -> None:
        if self._backtest_action_inflight:
            return
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_status_var.set(f"正在更新 {symbol} 的 K 线到最新…")

        def update() -> None:
            try:
                new_download_id = (
                    self.backend_client.update_historical_download(download_id)
                )
                self._enqueue_event(
                    (
                        "backtest_action",
                        "update",
                        new_download_id,
                        "",
                        download_id,
                    )
                )
            except Exception as exc:
                self._enqueue_event(
                    (
                        "backtest_action",
                        "update",
                        "",
                        str(exc),
                        download_id,
                    )
                )

        threading.Thread(
            target=update, name="historical-bars-update", daemon=True
        ).start()

    def _begin_historical_export(
        self, symbol: str, provider: str
    ) -> None:
        if self._backtest_action_inflight:
            return
        default_name = (
            f"{symbol}_{provider}_historical_klines.zip"
            if provider
            else f"{symbol}_historical_klines.zip"
        )
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "导出历史 K 线",
            default_name,
            "AutoQuant K线数据包 (*.zip)",
        )
        if not selected:
            return
        target = Path(selected)
        if target.suffix.lower() != ".zip":
            target = target.with_suffix(".zip")
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_status_var.set(f"正在导出 {symbol} 的历史 K 线…")

        def export() -> None:
            try:
                archive = self.backend_client.export_historical_bars(
                    symbol, provider
                )
                target.write_bytes(archive)
                self._enqueue_event(
                    ("backtest_archive", "export", str(target), {}, "")
                )
            except Exception as exc:
                self._enqueue_event(
                    ("backtest_archive", "export", str(target), {}, str(exc))
                )

        threading.Thread(
            target=export, name="backtest-bars-export", daemon=True
        ).start()

    def _delete_historical_bars(
        self,
        symbol: str,
        provider: str,
        item: dict[str, object],
    ) -> None:
        if self._backtest_action_inflight:
            return
        bar_count = sum(
            int(item.get(field, 0) or 0)
            for field in (
                "daily_count",
                "five_minute_count",
                "one_minute_count",
            )
        )
        if not ask_yes_no(
            "确认删除历史 K 线",
            f"将删除 {symbol}（{provider}）已持久化的约 {bar_count} 根 K 线，"
            "并清除该标的的下载/导入记录。\n\n"
            "已保存的回测结果不会删除。此操作不可撤销，确认继续吗？",
        ):
            return
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_status_var.set(f"正在删除 {symbol} 的历史 K 线…")

        def delete() -> None:
            try:
                result = self.backend_client.delete_historical_bars(
                    symbol, provider
                )
                self._enqueue_event(("backtest_delete", result, ""))
            except Exception as exc:
                self._enqueue_event(("backtest_delete", {}, str(exc)))

        threading.Thread(
            target=delete, name="backtest-bars-delete", daemon=True
        ).start()

    def _apply_backtest_delete(
        self, result: dict[str, object], error: str
    ) -> None:
        self._backtest_action_inflight = False
        self._set_backtest_actions_enabled(True)
        if error:
            self.backtest_status_var.set(f"历史 K 线删除失败：{error}")
            show_error("历史 K 线删除失败", error)
            return
        message = (
            f"已删除 {result.get('symbol', '')} 的 "
            f"{result.get('deleted_bars', 0)} 根历史 K 线和 "
            f"{result.get('deleted_downloads', 0)} 条下载/导入记录；"
            "回测结果已保留。"
        )
        self.backtest_status_var.set(message)
        show_info("删除完成", message)

    def _import_historical_bars(self) -> None:
        if self._backtest_action_inflight:
            return
        try:
            symbol = self._backtest_symbol()
        except ValueError as exc:
            show_error("无法导入", str(exc))
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "导入历史 K 线",
            "",
            "AutoQuant K线数据包 (*.zip)",
        )
        if not selected:
            return
        source = Path(selected)
        try:
            if source.stat().st_size > 128 * 1024 * 1024:
                raise ValueError("导入文件超过 128 MB 限制")
        except (OSError, ValueError) as exc:
            show_error("无法导入", str(exc))
            return
        self._backtest_action_inflight = True
        self._set_backtest_actions_enabled(False)
        self.backtest_status_var.set(f"正在导入 {symbol} 的历史 K 线…")

        def import_bars() -> None:
            try:
                result = self.backend_client.import_historical_bars(
                    source.read_bytes(), expected_symbol=symbol
                )
                self._enqueue_event(
                    ("backtest_archive", "import", str(source), result, "")
                )
            except Exception as exc:
                self._enqueue_event(
                    ("backtest_archive", "import", str(source), {}, str(exc))
                )

        threading.Thread(
            target=import_bars, name="backtest-bars-import", daemon=True
        ).start()

    def _apply_backtest_archive(
        self,
        action: str,
        path: str,
        result: dict[str, object],
        error: str,
    ) -> None:
        self._backtest_action_inflight = False
        self._set_backtest_actions_enabled(True)
        if error:
            title = "历史 K 线导出失败" if action == "export" else "历史 K 线导入失败"
            self.backtest_status_var.set(f"{title}：{error}")
            show_error(title, error)
            return
        if action == "export":
            message = f"历史 K 线已导出到：\n{path}"
            self.backtest_status_var.set(message.replace("\n", " "))
            show_info("导出完成", message)
        else:
            counts = result.get("counts", {})
            if not isinstance(counts, dict):
                counts = {}
            message = (
                f"{result.get('symbol', '')} 导入完成："
                f"日线 {counts.get('1d', 0)} 根、5分钟 {counts.get('5m', 0)} 根、"
                f"1分钟 {counts.get('1m', 0)} 根。"
            )
            self.backtest_status_var.set(message)
            show_info("导入完成", message)

    def _apply_backtest_action(
        self,
        action: str,
        identifier: str,
        error: str,
        source_download_id: str = "",
    ) -> None:
        self._backtest_action_inflight = False
        self._set_backtest_actions_enabled(True)
        if error:
            title = {
                "download": "历史数据下载失败",
                "update": "历史 K 线更新失败",
                "run": "回测启动失败",
            }.get(action, "操作失败")
            self.backtest_status_var.set(f"{title}：{error}")
            show_error(title, error)
            if source_download_id:
                active = self._backtest_active_runs.get(source_download_id)
                self.backtest_download_tree.set_action_state(
                    source_download_id,
                    action="stop" if active else "start",
                    enabled=True,
                )
        else:
            label = {
                "download": "下载",
                "update": "K 线更新",
                "run": "回测",
            }.get(action, "后台")
            self.backtest_status_var.set(
                f"{label}任务已提交（{identifier[:8]}），后台执行中。"
            )

    def _apply_backtest_stop(
        self, run_id: str, download_id: str, error: str
    ) -> None:
        self._backtest_action_inflight = False
        self._set_backtest_actions_enabled(True)
        if error:
            self.backtest_status_var.set(f"回测停止失败：{error}")
            show_error("回测停止失败", error)
            self.backtest_download_tree.set_action_state(
                download_id, action="stop", enabled=True
            )
            return
        self.backtest_status_var.set(
            f"回测任务 {run_id[:8]} 正在停止。"
        )

    def _refresh_backtest_data(self, *, manual: bool = False) -> None:
        if self._closed or self._backtest_refresh_inflight:
            return
        self._backtest_refresh_inflight = True
        if manual:
            self.backtest_refresh_button.setEnabled(False)

        def refresh() -> None:
            try:
                downloads = self.backend_client.historical_downloads()
                runs = self.backend_client.backtest_runs()
                self._enqueue_event(("backtest_data", downloads, runs, ""))
            except Exception as exc:
                self._enqueue_event(("backtest_data", [], [], str(exc)))

        threading.Thread(
            target=refresh, name="backtest-data-refresh", daemon=True
        ).start()

    @staticmethod
    def _backtest_datetime(timestamp: object) -> str:
        try:
            value = int(timestamp)
        except (TypeError, ValueError):
            return "—"
        if value <= 0:
            return "—"
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _backtest_metric(value: object, suffix: str = "") -> str:
        try:
            number = Decimal(str(value))
            if not number.is_finite():
                raise ValueError
        except (ArithmeticError, ValueError):
            return "—"
        return f"{number:.2f}{suffix}"

    def _load_backtest_trade_details(
        self, run_id: str, summary: dict[str, object]
    ) -> None:
        if self._backtest_detail_inflight:
            return
        self._backtest_detail_inflight = True
        self.backtest_status_var.set(
            f"正在读取 {summary.get('symbol', '')} 的回测明细…"
        )

        def load() -> None:
            try:
                items = self.backend_client.backtest_trade_details(run_id)
                self._enqueue_event(
                    ("backtest_details", summary, items, "")
                )
            except Exception as exc:
                self._enqueue_event(
                    ("backtest_details", summary, [], str(exc))
                )

        threading.Thread(
            target=load, name="backtest-trade-details", daemon=True
        ).start()

    def _apply_backtest_trade_details(
        self,
        summary: dict[str, object],
        items: list[dict[str, object]],
        error: str,
    ) -> None:
        self._backtest_detail_inflight = False
        if error:
            self.backtest_status_var.set(f"回测明细读取失败：{error}")
            show_error("回测明细读取失败", error)
            return
        self.backtest_status_var.set(
            f"已读取 {summary.get('symbol', '')} 的 {len(items)} 笔回测明细。"
        )
        self._show_backtest_trade_detail_dialog(summary, items)

    def _build_backtest_pnl_chart(
        self,
        items: list[dict[str, object]],
        currency: str,
    ) -> tuple[QChartView, QLabel]:
        cumulative_values: list[tuple[int, float, Decimal]] = []
        cumulative = Decimal("0")
        for index, item in enumerate(items, start=1):
            try:
                pnl = Decimal(str(item.get("pnl", "0")))
                if not pnl.is_finite():
                    pnl = Decimal("0")
            except ArithmeticError:
                pnl = Decimal("0")
            cumulative += pnl
            cumulative_values.append((index, float(cumulative), pnl))

        max_points = 2000
        step = max(1, (len(cumulative_values) + max_points - 1) // max_points)
        sampled_indices = list(range(0, len(cumulative_values), step))
        if cumulative_values and sampled_indices[-1] != len(cumulative_values) - 1:
            sampled_indices.append(len(cumulative_values) - 1)

        curve = QLineSeries()
        curve.setName("累计盈亏")
        curve_pen = QPen(QColor(COLORS["primary"]), 2.2)
        curve.setPen(curve_pen)
        curve.append(0, 0)
        wins = QScatterSeries()
        wins.setName("盈利交易")
        wins.setColor(QColor(COLORS["positive"]))
        wins.setBorderColor(QColor(COLORS["positive"]))
        wins.setMarkerSize(6)
        losses = QScatterSeries()
        losses.setName("亏损交易")
        losses.setColor(QColor(COLORS["negative"]))
        losses.setBorderColor(QColor(COLORS["negative"]))
        losses.setMarkerSize(6)
        for sampled_index in sampled_indices:
            trade_number, equity, pnl = cumulative_values[sampled_index]
            curve.append(trade_number, equity)
            if pnl > 0:
                wins.append(trade_number, equity)
            elif pnl < 0:
                losses.append(trade_number, equity)

        count = len(cumulative_values)
        zero = QLineSeries()
        zero.setName("盈亏零轴")
        zero.append(0, 0)
        zero.append(max(1, count), 0)
        zero.setPen(
            QPen(QColor("#98a2b3"), 1, Qt.PenStyle.DashLine)
        )
        guide = QLineSeries()
        guide.setName("当前交易")
        guide.setPen(
            QPen(QColor(COLORS["primary"]), 1, Qt.PenStyle.DashLine)
        )
        guide.setVisible(False)

        chart = QChart()
        chart.addSeries(curve)
        chart.addSeries(wins)
        chart.addSeries(losses)
        chart.addSeries(zero)
        chart.addSeries(guide)
        sample_note = (
            f"，抽样显示 {len(sampled_indices)}/{count} 个点"
            if count > max_points
            else ""
        )
        chart.setTitle(
            f"累计盈亏曲线（{currency}，共 {count} 笔{sample_note}）"
        )
        chart.setBackgroundVisible(False)
        chart.setPlotAreaBackgroundVisible(True)
        chart.setPlotAreaBackgroundBrush(QColor("#fbfcfe"))
        chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        for marker in chart.legend().markers(zero):
            marker.setVisible(False)
        for marker in chart.legend().markers(guide):
            marker.setVisible(False)

        axis_x = QValueAxis()
        axis_x.setTitleText("交易序号")
        axis_x.setRange(0, max(1, count))
        axis_x.setLabelFormat("%d")
        axis_x.setTickCount(min(11, max(2, count + 1)))
        all_equity = [0.0, *(value for _index, value, _pnl in cumulative_values)]
        minimum = min(all_equity)
        maximum = max(all_equity)
        span = maximum - minimum
        padding = max(span * 0.12, max(abs(minimum), abs(maximum), 1.0) * 0.05)
        axis_y = QValueAxis()
        axis_y.setTitleText(f"累计盈亏（{currency}）")
        axis_y.setRange(minimum - padding, maximum + padding)
        axis_y.setLabelFormat("%.2f")
        axis_y.setTickCount(6)
        chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        for series in (curve, wins, losses, zero, guide):
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)

        view = InteractiveChartView(chart)
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setMinimumHeight(250)
        view.setToolTip(
            "蓝线为逐笔累计盈亏；绿点表示盈利交易，红点表示亏损交易。"
            "将鼠标悬停或点击曲线上的点可查看对应交易。"
        )
        detail = QLabel(
            "将鼠标悬停或点击图表中的曲线、盈利点或亏损点查看对应交易明细。"
        )
        detail.setObjectName("backtestChartDetail")
        detail.setWordWrap(True)
        detail.setMinimumHeight(68)
        detail.setStyleSheet(
            f"""
            QLabel#backtestChartDetail {{
                color: {COLORS['text']};
                background: #f7faff;
                border: 1px solid #cfe0f7;
                border-radius: 7px;
                padding: 8px 11px;
            }}
            """
        )
        side_text = {"LONG": "多头", "SHORT": "空头"}
        exit_text = {
            "STOP_LOSS": "止损",
            "TAKE_PROFIT": "止盈",
            "END_OF_DATA": "数据结束",
        }

        def show_point(point: QPointF, *, clicked: bool) -> None:
            if not items:
                return
            trade_number = min(
                len(items), max(1, int(point.x() + 0.5))
            )
            item = items[trade_number - 1]
            pnl = Decimal(str(item.get("pnl", "0")))
            cumulative_pnl = Decimal(
                str(cumulative_values[trade_number - 1][1])
            )
            direction = side_text.get(
                str(item.get("side", "")), str(item.get("side", ""))
            )
            reason = exit_text.get(
                str(item.get("exit_reason", "")),
                str(item.get("exit_reason", "")),
            )
            color = (
                COLORS["positive"]
                if pnl > 0
                else COLORS["negative"]
                if pnl < 0
                else COLORS["text"]
            )
            state = "已选择" if clicked else "当前悬停"
            guide.clear()
            guide.append(trade_number, axis_y.min())
            guide.append(trade_number, axis_y.max())
            guide.setVisible(True)
            detail.setText(
                f"<b>{state}：第 {trade_number} 笔 · {direction}</b>　"
                f"开仓 {self._backtest_datetime(item.get('entry_time'))} "
                f"@ {self._backtest_metric(item.get('entry_price', '0'))}　"
                f"平仓 {self._backtest_datetime(item.get('exit_time'))} "
                f"@ {self._backtest_metric(item.get('exit_price', '0'))}<br>"
                f"数量 {self._backtest_metric(item.get('quantity', '0'))}　"
                f"单笔盈亏 <span style='color:{color}; font-weight:600'>"
                f"{self._backtest_metric(pnl)} {currency}</span>　"
                f"累计盈亏 {self._backtest_metric(cumulative_pnl)} {currency}　"
                f"退出原因：{reason}"
            )
            QToolTip.showText(
                QCursor.pos(),
                f"第 {trade_number} 笔 · {direction}\n"
                f"单笔盈亏 {self._backtest_metric(pnl)} {currency}\n"
                f"累计盈亏 {self._backtest_metric(cumulative_pnl)} {currency}\n"
                f"退出原因：{reason}",
                view,
            )

        def hover_point(point: QPointF, hovered: bool) -> None:
            if hovered:
                show_point(point, clicked=False)
            else:
                QToolTip.hideText()

        for series in (curve, wins, losses):
            series.hovered.connect(hover_point)
            series.clicked.connect(
                lambda point, _series=series: show_point(point, clicked=True)
            )
        view.set_point_callback(
            lambda point, clicked: show_point(point, clicked=clicked)
        )
        return view, detail

    def _show_backtest_trade_detail_dialog(
        self,
        summary: dict[str, object],
        items: list[dict[str, object]],
    ) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(
            f"回测明细 - {summary.get('symbol', '')}"
        )
        dialog.resize(1180, 680)
        layout = QVBoxLayout(dialog)
        provider = str(summary.get("provider", ""))
        currency = "USDT" if provider == "binance_futures" else "USDC"
        total_pnl = self._backtest_metric(summary.get("total_pnl", "0"))
        overview = QLabel(
            f"标的：{summary.get('symbol', '')}    "
            f"策略：{summary.get('strategy', '')}    "
            f"交易：{summary.get('trade_count', len(items))} 笔    "
            f"胜/负：{summary.get('win_count', 0)}/{summary.get('loss_count', 0)}    "
            f"总盈亏：{total_pnl} {currency}    "
            f"收益率：{self._backtest_metric(summary.get('return_percent', '0'), '%')}    "
            f"最大回撤：{self._backtest_metric(summary.get('max_drawdown_percent', '0'), '%')}"
        )
        overview.setWordWrap(True)
        overview.setStyleSheet(f"color: {COLORS['muted']};")
        layout.addWidget(overview)

        chart_view, chart_detail = self._build_backtest_pnl_chart(
            items, currency
        )

        table = KeyedTable(
            [
                "开仓时间", "平仓时间", "方向", "开仓价", "平仓价",
                "数量", "盈亏", "退出原因", "信号原因",
            ],
            [165, 165, 65, 85, 85, 80, 90, 95, 330],
            multi_select=False,
        )
        side_text = {"LONG": "多头", "SHORT": "空头"}
        exit_text = {
            "STOP_LOSS": "止损",
            "TAKE_PROFIT": "止盈",
            "END_OF_DATA": "数据结束",
        }
        for index, item in enumerate(items):
            pnl = Decimal(str(item.get("pnl", "0")))
            table.insert(
                "",
                None,
                iid=str(item.get("trade_id", index)),
                text=self._backtest_datetime(item.get("entry_time")),
                values=(
                    self._backtest_datetime(item.get("exit_time")),
                    side_text.get(str(item.get("side", "")), str(item.get("side", ""))),
                    self._backtest_metric(item.get("entry_price", "0")),
                    self._backtest_metric(item.get("exit_price", "0")),
                    self._backtest_metric(item.get("quantity", "0")),
                    f"{self._backtest_metric(pnl)} {currency}",
                    exit_text.get(
                        str(item.get("exit_reason", "")),
                        str(item.get("exit_reason", "")),
                    ),
                    item.get("signal_reason", ""),
                ),
                tags=("win",) if pnl > 0 else ("loss",) if pnl < 0 else (),
            )
            row = table.rowCount() - 1
            for column in range(table.columnCount()):
                cell = table.item(row, column)
                header = table.horizontalHeaderItem(column)
                if cell is not None:
                    title = header.text() if header is not None else "明细"
                    cell.setToolTip(f"{title}：{cell.text()}")
        table.setMouseTracking(True)
        table.viewport().setMouseTracking(True)
        pages = QTabWidget(dialog)
        pages.setDocumentMode(True)
        chart_page = QWidget(pages)
        chart_layout = QVBoxLayout(chart_page)
        chart_layout.setContentsMargins(6, 6, 6, 6)
        chart_layout.addWidget(chart_view, 1)
        chart_layout.addWidget(chart_detail)
        table_page = QWidget(pages)
        table_layout = QVBoxLayout(table_page)
        table_layout.setContentsMargins(6, 6, 6, 6)
        table_layout.addWidget(table, 1)
        if not items:
            empty = QLabel("该回测没有产生交易明细。")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {COLORS['muted']};")
            table_layout.addWidget(empty)
        config_page = QWidget(pages)
        config_layout = QGridLayout(config_page)
        config_layout.setContentsMargins(18, 18, 18, 18)
        config_layout.setHorizontalSpacing(18)
        config_layout.setVerticalSpacing(12)
        config_layout.setColumnStretch(1, 1)
        raw_strategy_config = summary.get("strategy_config", {})
        strategy_config = (
            raw_strategy_config
            if isinstance(raw_strategy_config, dict)
            else {}
        )
        if strategy_config:
            strategy_name = str(
                strategy_config.get("strategy_name")
                or strategy_config.get("strategy")
                or summary.get("strategy", "")
            )
            fast_ma = strategy_config.get("fast_ma_period", "—")
            slow_ma = strategy_config.get("slow_ma_period", "—")
            config_rows = (
                ("策略", strategy_name),
                ("K线周期", strategy_config.get("kline_interval", "—")),
                ("策略均线", f"MA{fast_ma} / MA{slow_ma}"),
                (
                    f"开仓金额({currency})",
                    strategy_config.get("buy_notional", "—"),
                ),
                (
                    f"单笔上限({currency})",
                    strategy_config.get("max_order_notional", "—"),
                ),
                (
                    f"每日开仓上限({currency})",
                    strategy_config.get("max_daily_buy_notional", "—"),
                ),
                (
                    "持仓加仓次数",
                    strategy_config.get("max_additions_per_position", "—"),
                ),
                (
                    "止损/止盈(%)",
                    f"{strategy_config.get('stop_loss_percent', '—')} / "
                    f"{strategy_config.get('take_profit_percent', '—')}",
                ),
                (
                    "信号有效期(秒)",
                    strategy_config.get("max_signal_age_seconds", "—"),
                ),
                (
                    "AI时机K线数量",
                    strategy_config.get("ai_entry_timing_bars", "—"),
                ),
            )
            for row, (title, value) in enumerate(config_rows):
                title_label = QLabel(str(title))
                title_label.setStyleSheet("font-weight: 600;")
                value_label = QLabel(str(value))
                value_label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
                config_layout.addWidget(title_label, row, 0)
                config_layout.addWidget(value_label, row, 1)
            snapshot_note = QLabel(
                "这是提交本次回测时保存的策略配置副本；后续修改策略配置不会改变这里的值。"
            )
            snapshot_note.setObjectName("backtestStrategyConfigNote")
            snapshot_note.setWordWrap(True)
            snapshot_note.setStyleSheet(f"color: {COLORS['muted']};")
            config_layout.addWidget(snapshot_note, len(config_rows), 0, 1, 2)
            config_layout.setRowStretch(len(config_rows) + 1, 1)
        else:
            empty_config = QLabel("该历史回测没有保存策略配置副本。")
            empty_config.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_config.setStyleSheet(f"color: {COLORS['muted']};")
            config_layout.addWidget(empty_config, 0, 0, 1, 2)
            config_layout.setRowStretch(1, 1)
        pages.addTab(chart_page, "收益曲线")
        pages.addTab(table_page, f"交易明细 ({len(items)})")
        pages.addTab(config_page, "策略配置副本")
        layout.addWidget(pages, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        self._backtest_detail_dialog = dialog
        dialog.show()

    def _apply_backtest_data(
        self,
        downloads: list[dict[str, object]],
        runs: list[dict[str, object]],
        error: str,
        *,
        refresh_complete: bool = True,
    ) -> None:
        if refresh_complete:
            self._backtest_refresh_inflight = False
            self.backtest_refresh_button.setEnabled(True)
        if error:
            self.backtest_status_var.set(f"回测记录刷新失败：{error}")
            return
        status_text = {
            "QUEUED": "排队中",
            "RUNNING": "进行中",
            "STOPPING": "停止中",
            "CANCELLED": "已停止",
            "COMPLETED": "已完成",
            "FAILED": "失败",
        }
        self._backtest_downloads = {
            str(item.get("download_id", "")): dict(item)
            for item in downloads
            if str(item.get("download_id", ""))
        }
        active_run_statuses = {"QUEUED", "RUNNING", "STOPPING"}
        self._backtest_active_runs = {
            str(item.get("download_id", "")): dict(item)
            for item in runs
            if str(item.get("download_id", ""))
            and str(item.get("status", "")) in active_run_statuses
        }
        self.backtest_download_tree.clear_rows()
        for item in downloads:
            key = str(item.get("download_id", ""))
            status = str(item.get("status", ""))
            active_run = self._backtest_active_runs.get(key)
            self.backtest_download_tree.insert(
                "", None, iid=key, text=str(item.get("symbol", "")),
                values=(
                    "",
                    self._backtest_datetime(item.get("created_at")),
                    self._backtest_datetime(
                        item.get("updated_at") or item.get("created_at")
                    ),
                    item.get("provider", ""),
                    status_text.get(status, status), f"{item.get('progress', 0)}%",
                    item.get("daily_count", 0), item.get("five_minute_count", 0),
                    item.get("one_minute_count", 0), item.get("message", ""),
                ),
                tags=("error",) if status == "FAILED" else ("running",) if status == "RUNNING" else (),
            )
            self.backtest_download_tree.set_action_button(
                key,
                BACKTEST_DOWNLOAD_ACTION_COLUMN,
                lambda _checked=False, download_id=key:
                self._start_download_backtest(download_id),
                lambda _checked=False, download_id=key:
                self._stop_download_backtest(download_id),
                start_verb="启动回测",
                stop_verb="停止回测",
                action_subject=str(item.get("symbol", "")),
            )
            self.backtest_download_tree.set_action_state(
                key,
                action="stop" if active_run is not None else "start",
                enabled=(
                    str(active_run.get("status", "")) != "STOPPING"
                    if active_run is not None
                    else status == "COMPLETED"
                ),
            )
        self.backtest_run_tree.clear_rows()
        for item in runs:
            key = str(item.get("run_id", ""))
            status = str(item.get("status", ""))
            provider = str(item.get("provider", ""))
            currency = (
                "USDT"
                if provider == "binance_futures"
                else "USDC"
                if provider == "binance_stocks"
                else ""
            )
            total_pnl = self._backtest_metric(item.get("total_pnl", "0"))
            if currency:
                total_pnl += f" {currency}"
            self.backtest_run_tree.insert(
                "", None, iid=key,
                text=self._backtest_datetime(
                    item.get("completed_at") or item.get("created_at")
                ),
                values=(
                    item.get("symbol", ""), item.get("strategy", ""),
                    status_text.get(status, status), item.get("trade_count", 0),
                    f"{item.get('win_count', 0)}/{item.get('loss_count', 0)}",
                    total_pnl,
                    self._backtest_metric(item.get("return_percent", "0"), "%"),
                    self._backtest_metric(
                        item.get("max_drawdown_percent", "0"), "%"
                    ),
                    "",
                    item.get("message", ""),
                ),
                tags=("error",) if status == "FAILED" else ("running",) if status in active_run_statuses else (),
            )
            detail_button = QPushButton("回测明细", self.backtest_run_tree)
            detail_button.setFlat(True)
            detail_button.setCursor(Qt.CursorShape.PointingHandCursor)
            detail_button.setStyleSheet(
                f"""
                QPushButton {{
                    min-height: 24px;
                    padding: 0;
                    color: {COLORS['primary']};
                    background: transparent;
                    border: none;
                    font-weight: 600;
                }}
                QPushButton:hover {{
                    color: {COLORS['primary_hover']};
                    background: transparent;
                    border: none;
                    text-decoration: underline;
                }}
                QPushButton:pressed {{
                    color: {COLORS['primary_hover']};
                    background: transparent;
                    border: none;
                }}
                QPushButton:disabled {{
                    color: #98a2b3;
                    background: transparent;
                    border: none;
                }}
                """
            )
            detail_button.setEnabled(status == "COMPLETED")
            detail_button.setToolTip("查看该回测批次的逐笔开平仓明细")
            detail_button.clicked.connect(
                lambda _checked=False, run_id=key, summary=dict(item):
                self._load_backtest_trade_details(run_id, summary)
            )
            self.backtest_run_tree.setCellWidget(
                self.backtest_run_tree.rowCount() - 1, 9, detail_button
            )
        active_downloads = sum(
            1 for item in downloads if item.get("status") in {"QUEUED", "RUNNING"}
        )
        active_runs = sum(
            1 for item in runs if item.get("status") in active_run_statuses
        )
        self.backtest_status_var.set(
            f"已加载 {len(downloads)} 条下载记录、{len(runs)} 条回测结果；"
            f"进行中的下载 {active_downloads} 个、回测 {active_runs} 个。"
        )

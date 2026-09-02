from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from autoquant_frontend.client import BackendClientError
from autoquant_frontend.constants import CONTRACT_POOL_REFRESH_MS
from autoquant_frontend.dialogs import show_error, show_info
from autoquant_frontend.theme import COLORS
from autoquant_frontend.widgets import KeyedTable
from autoquant_shared.config import MAX_CONTRACT_POOL_SYMBOLS


class ContractPoolPageMixin:
    """Contract discovery, ranking, and pool management page."""

    def _build_contract_pool_page(self) -> None:
        layout = QVBoxLayout(self.contract_pool_page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        controls = QHBoxLayout()
        controls.addStretch()
        self.contract_pool_refresh_button = self._button(
            "刷新涨跌榜", self._refresh_futures_rankings
        )
        controls.addWidget(self.contract_pool_refresh_button)
        layout.addLayout(controls)

        content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.contract_pool_tree = KeyedTable(
            ["合约", "24h 涨跌幅"], [150, 120], multi_select=True
        )
        self.contract_pool_tree.verticalHeader().setDefaultSectionSize(34)
        self.contract_pool_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.contract_pool_tree.customContextMenuRequested.connect(
            self._show_contract_pool_context_menu
        )
        content_splitter.addWidget(self.contract_pool_tree)

        self.futures_ranking_tabs = QTabWidget()
        self.futures_ranking_tabs.setDocumentMode(True)
        self.stock_gainers_tree = self._add_ranking_tab("股票涨幅榜")
        self.stock_losers_tree = self._add_ranking_tab("股票跌幅榜")
        self.crypto_gainers_tree = self._add_ranking_tab("加密涨幅榜")
        self.crypto_losers_tree = self._add_ranking_tab("加密跌幅榜")
        content_splitter.addWidget(self.futures_ranking_tabs)
        content_splitter.setStretchFactor(0, 1)
        content_splitter.setStretchFactor(1, 3)
        content_splitter.setSizes([300, 900])
        layout.addWidget(content_splitter, 1)
        self._sync_contract_pool_table()

    def _add_ranking_tab(self, title: str) -> KeyedTable:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(8, 8, 8, 8)
        table = KeyedTable(
            ["合约", "24h 涨跌幅", "最新价", "24h 成交额(USDT)"],
            [110, 110, 120, 160],
            multi_select=True,
        )
        table.verticalHeader().setDefaultSectionSize(34)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda position, target=table: self._show_rankings_context_menu(
                target, position
            )
        )
        page_layout.addWidget(table)
        self.futures_ranking_tabs.addTab(page, title)
        return table

    def _sync_contract_pool_refresh_timer(self) -> None:
        should_run = (
            not self._closed
            and self.notebook.currentWidget() is self.contract_pool_page
            and bool(self.config.contract_pool)
        )
        if should_run:
            if not self.contract_pool_timer.isActive():
                self.contract_pool_timer.start(CONTRACT_POOL_REFRESH_MS)
        else:
            self.contract_pool_timer.stop()

    def _auto_refresh_futures_rankings(self) -> None:
        if self.notebook.currentWidget() is self.contract_pool_page:
            self._refresh_futures_rankings()

    def _auto_refresh_contract_pool(self) -> None:
        if (
            self.notebook.currentWidget() is self.contract_pool_page
            and self._futures_rankings_loaded
            and self.config.contract_pool
        ):
            self._refresh_contract_pool_tickers()

    def _refresh_contract_pool_tickers(self) -> None:
        if (
            self._closed
            or self._contract_pool_refresh_inflight
            or self._futures_rankings_inflight
            or not self.config.contract_pool
        ):
            return
        self._contract_pool_refresh_inflight = True

        def load() -> None:
            try:
                payload = self.backend_client.futures_rankings(limit=1)
                tickers = payload.get("tickers", {})
                self._enqueue_event(("contract_pool_tickers", tickers, ""))
            except Exception as exc:
                self._enqueue_event(
                    (
                        "contract_pool_tickers",
                        {},
                        str(exc) or exc.__class__.__name__,
                    )
                )

        threading.Thread(
            target=load,
            name="contract-pool-tickers-loader",
            daemon=True,
        ).start()

    def _apply_contract_pool_tickers(
        self, tickers: object, error: str
    ) -> None:
        self._contract_pool_refresh_inflight = False
        if error or not isinstance(tickers, dict):
            return
        self._futures_tickers.update(
            {
                str(symbol).strip().upper(): dict(item)
                for symbol, item in tickers.items()
                if str(symbol).strip() and isinstance(item, dict)
            }
        )
        self._sync_contract_pool_table()

    def _show_rankings_context_menu(
        self, table: KeyedTable, position: QPoint
    ) -> None:
        index = table.indexAt(position)
        if not index.isValid():
            return
        clicked_symbol = table.get_children()[index.row()]
        if clicked_symbol not in table.selection():
            table.clearSelection()
            table.selectRow(index.row())
        menu = QMenu(table)
        add_action = menu.addAction("添加")
        add_action.triggered.connect(
            lambda _checked=False, selected_table=table:
            self._add_selected_rankings_to_pool(selected_table)
        )
        menu.exec(table.viewport().mapToGlobal(position))

    def _show_contract_pool_context_menu(self, position: QPoint) -> None:
        index = self.contract_pool_tree.indexAt(position)
        if not index.isValid():
            return
        clicked_symbol = self.contract_pool_tree.get_children()[index.row()]
        if clicked_symbol not in self.contract_pool_tree.selection():
            self.contract_pool_tree.clearSelection()
            self.contract_pool_tree.selectRow(index.row())
        menu = QMenu(self.contract_pool_tree)
        backtest_action = menu.addAction("回测")
        quant_action = menu.addAction("启动量化")
        menu.addSeparator()
        remove_action = menu.addAction("移除")
        backtest_action.triggered.connect(
            lambda _checked=False, symbol=clicked_symbol:
            self._open_contract_backtest(symbol)
        )
        quant_action.triggered.connect(
            lambda _checked=False, symbol=clicked_symbol:
            self._open_contract_quant(symbol)
        )
        remove_action.triggered.connect(
            lambda _checked=False: self._remove_selected_pool_contracts()
        )
        menu.exec(self.contract_pool_tree.viewport().mapToGlobal(position))

    def _open_contract_backtest(self, symbol: str) -> None:
        self.backtest_symbol_var.set(symbol)
        self.notebook.setCurrentWidget(self.backtest_page)

    def _open_contract_quant(self, symbol: str) -> None:
        self.notebook.setCurrentWidget(self.main_page)
        self.symbol_var.set(symbol)
        self._add_symbols()
        if not self.tree.exists(symbol):
            return
        self.tree.clearSelection()
        row = self.tree.get_children().index(symbol)
        self.tree.selectRow(row)
        item = self.tree.item(row, 0)
        if item is not None:
            self.tree.scrollToItem(item)

    def _refresh_futures_rankings(self) -> None:
        if self._closed or self._futures_rankings_inflight:
            return
        self._futures_rankings_inflight = True
        self.contract_pool_refresh_button.setEnabled(False)
        self.contract_pool_status_var.set("正在获取 Binance 合约行情……")

        def load() -> None:
            try:
                payload = self.backend_client.futures_rankings(limit=20)
                self._enqueue_event(("futures_rankings", payload, ""))
            except Exception as exc:
                self._enqueue_event(
                    (
                        "futures_rankings",
                        {},
                        str(exc) or exc.__class__.__name__,
                    )
                )

        threading.Thread(
            target=load,
            name="futures-rankings-loader",
            daemon=True,
        ).start()

    @staticmethod
    def _ranking_number(value: object, *, volume: bool = False) -> str:
        try:
            number = Decimal(str(value))
        except (ArithmeticError, ValueError):
            return "-"
        if not number.is_finite():
            return "-"
        if volume:
            return f"{number:,.0f}"
        places = 4 if abs(number) >= 1 else 8
        return f"{number:.{places}f}".rstrip("0").rstrip(".")

    def _apply_futures_rankings(
        self, payload: dict[str, object], error: str
    ) -> None:
        self._futures_rankings_inflight = False
        self.contract_pool_refresh_button.setEnabled(True)
        if error:
            self.contract_pool_status_var.set(f"涨跌榜刷新失败：{error}")
            return

        ranking_rows = (
            (self.stock_gainers_tree, payload.get("stock_gainers", []), "win"),
            (self.stock_losers_tree, payload.get("stock_losers", []), "loss"),
            (self.crypto_gainers_tree, payload.get("crypto_gainers", []), "win"),
            (self.crypto_losers_tree, payload.get("crypto_losers", []), "loss"),
        )
        tickers = payload.get("tickers", {})
        if any(not isinstance(rows, list) for _table, rows, _tag in ranking_rows):
            self.contract_pool_status_var.set("涨跌榜刷新失败：后端返回格式不正确")
            return
        if isinstance(tickers, dict):
            self._futures_tickers = {
                str(symbol).strip().upper(): dict(item)
                for symbol, item in tickers.items()
                if str(symbol).strip() and isinstance(item, dict)
            }
        for table, rows, tag in ranking_rows:
            table.clear_rows()
            for item in rows:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip().upper()
                if not symbol or table.exists(symbol):
                    continue
                change = self._ranking_number(item.get("price_change_percent"))
                if change != "-" and not change.startswith("-"):
                    change = "+" + change
                table.insert(
                    "",
                    None,
                    iid=symbol,
                    text=symbol,
                    values=(
                        f"{change}%",
                        self._ranking_number(item.get("last_price")),
                        self._ranking_number(
                            item.get("quote_volume"), volume=True
                        ),
                    ),
                    tags=(tag,),
                )
        timestamp = payload.get("updated_at")
        try:
            refreshed_at = datetime.fromtimestamp(
                int(timestamp) / 1000
            ).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            refreshed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._futures_rankings_loaded = True
        self._sync_contract_pool_refresh_timer()
        self._sync_contract_pool_table()
        self.contract_pool_status_var.set(
            f"更新于 {refreshed_at}；股票涨/跌 "
            f"{self.stock_gainers_tree.rowCount()}/{self.stock_losers_tree.rowCount()} 个，"
            f"加密涨/跌 {self.crypto_gainers_tree.rowCount()}/"
            f"{self.crypto_losers_tree.rowCount()} 个。涨跌幅为滚动 24 小时数据，"
            "每 30 分钟自动刷新。"
        )

    def _sync_contract_pool_table(self) -> None:
        self.contract_pool_tree.clear_rows()
        for symbol in self.config.contract_pool:
            ticker = self._futures_tickers.get(symbol, {})
            change = self._ranking_number(ticker.get("price_change_percent"))
            if change != "-" and not change.startswith("-"):
                change = "+" + change
            change_text = "-" if change == "-" else f"{change}%"
            self.contract_pool_tree.insert(
                "", None, iid=symbol, text=symbol, values=(change_text,)
            )
            if change != "-":
                self.contract_pool_tree.set_cell_foreground(
                    symbol,
                    1,
                    COLORS["negative"]
                    if change.startswith("-")
                    else COLORS["positive"],
                )

    def _add_selected_rankings_to_pool(self, table: KeyedTable) -> None:
        selected = list(table.selection())
        if not selected:
            show_info("请选择合约", "请先在涨跌榜中选择至少一个合约。")
            return
        current = list(self.config.contract_pool)
        existing = set(current)
        added = [symbol for symbol in selected if symbol not in existing]
        if not added:
            show_info("无需添加", "所选合约已经在合约池中。")
            return
        if len(current) + len(added) > MAX_CONTRACT_POOL_SYMBOLS:
            show_error(
                "添加合约失败",
                f"合约池数量不能超过 {MAX_CONTRACT_POOL_SYMBOLS} 个。",
            )
            return
        try:
            updated = replace(self.config, contract_pool=[*current, *added])
            updated.validate()
            self.store.save(updated)
            self.config = updated
            self._sync_contract_pool_table()
            self._sync_contract_pool_refresh_timer()
            self.contract_pool_status_var.set(
                f"已添加 {', '.join(added)}；当前合约池共 {len(updated.contract_pool)} 个。"
            )
        except (BackendClientError, OSError, TypeError, ValueError) as exc:
            show_error("添加合约失败", str(exc))

    def _remove_selected_pool_contracts(self) -> None:
        selected = list(self.contract_pool_tree.selection())
        if not selected:
            show_info("请选择合约", "请先在合约池中选择至少一个合约。")
            return
        try:
            removed = set(selected)
            updated = replace(
                self.config,
                contract_pool=[
                    symbol
                    for symbol in self.config.contract_pool
                    if symbol not in removed
                ],
            )
            self.store.save(updated)
            self.config = updated
            self._sync_contract_pool_table()
            self._sync_contract_pool_refresh_timer()
            self.contract_pool_status_var.set(
                f"已移除 {', '.join(selected)}；当前合约池共 {len(updated.contract_pool)} 个。"
            )
        except (BackendClientError, OSError, TypeError, ValueError) as exc:
            show_error("移除合约失败", str(exc))

from __future__ import annotations

import os
import queue
import re
import threading
import time
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, StringVar, Tk, messagebox
from tkinter import ttk

from autoquant.config import MAX_SYMBOLS, AppConfig, ConfigStore, normalize_symbols
from autoquant.engine import RunnerConfig, TradingController, create_provider
from autoquant.models import AccountOverview, RunState, RuntimeSnapshot


ACCOUNT_REFRESH_MS = 30_000


STATE_TEXT = {
    RunState.STOPPED: "已停止",
    RunState.STARTING: "启动中",
    RunState.WARMING_UP: "预热中",
    RunState.RUNNING: "运行中",
    RunState.SIGNAL: "信号",
    RunState.ERROR: "错误",
    RunState.STOPPING: "停止中",
}


class AutoQuantApp:
    def __init__(self, root: Tk, config_store: ConfigStore | None = None) -> None:
        self.root = root
        self.root.title("AutoQuant - Binance Stocks 量化控制台")
        self.root.geometry("1180x760")
        self.root.minsize(980, 650)
        self.store = config_store or ConfigStore()
        self.events: queue.Queue[tuple] = queue.Queue(maxsize=1000)
        self.config = self._load_config()

        self.provider_var = StringVar(value=self.config.provider)
        self.strategy_var = StringVar(value=self.config.strategy)
        self.mode_var = StringVar(value=self.config.trading_mode)
        self.api_key_var = StringVar(value=os.environ.get("BINANCE_API_KEY", ""))
        self.api_secret_var = StringVar(
            value=os.environ.get("BINANCE_API_SECRET", "")
        )
        self.ma_var = StringVar(value=str(self.config.ma_period))
        self.buy_notional_var = StringVar(value=self.config.buy_notional)
        self.sell_quantity_var = StringVar(value=self.config.sell_quantity)
        self.max_trades_var = StringVar(value=str(self.config.max_trades_per_day))
        self.max_order_notional_var = StringVar(
            value=self.config.max_order_notional
        )
        self.max_daily_buy_notional_var = StringVar(
            value=self.config.max_daily_buy_notional
        )
        self.stop_loss_var = StringVar(value=self.config.stop_loss_percent)
        self.take_profit_var = StringVar(value=self.config.take_profit_percent)
        self.max_signal_age_var = StringVar(
            value=str(self.config.max_signal_age_seconds)
        )
        self.symbol_var = StringVar()
        self.account_total_var = StringVar(value="—")
        self.realized_pnl_var = StringVar(value="0.00 USDC")
        self.unrealized_pnl_var = StringVar(value="0.00 USDC")
        self.account_status_var = StringVar(value="等待首次刷新")
        self._latest_prices: dict[str, Decimal] = {}
        self._account_refresh_inflight = False
        self._closed = False

        self.controller = TradingController(
            snapshot_callback=lambda snapshot: self._enqueue_event(
                ("snapshot", snapshot)
            ),
            log_callback=lambda level, symbol, message: self._enqueue_event(
                ("log", level, symbol, message)
            ),
        )
        self._build_ui()
        for symbol in self.config.symbols:
            self._insert_symbol(symbol)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_events)
        self.root.after(500, self._account_refresh_tick)

    def _load_config(self) -> AppConfig:
        try:
            return self.store.load()
        except ValueError as exc:
            message = str(exc)
            self.root.after(
                0,
                lambda message=message: messagebox.showwarning(
                    "配置警告", message
                ),
            )
            return AppConfig()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.main_page = ttk.Frame(self.notebook)
        self.config_page = ttk.Frame(self.notebook)
        self.notebook.add(self.main_page, text="交易监控")
        self.notebook.add(self.config_page, text="运行配置")

        self.main_page.columnconfigure(0, weight=1)
        self.main_page.rowconfigure(2, weight=1)
        self.main_page.rowconfigure(4, weight=1)
        self.config_page.columnconfigure(0, weight=1)

        style = ttk.Style(self.root)
        style.configure("AccountValue.TLabel", font=("Segoe UI", 22, "bold"))
        style.configure(
            "AccountPositive.TLabel",
            font=("Segoe UI", 22, "bold"),
            foreground="#087830",
        )
        style.configure(
            "AccountNegative.TLabel",
            font=("Segoe UI", 22, "bold"),
            foreground="#b00020",
        )

        overview = ttk.LabelFrame(
            self.main_page, text="账户与程序盈亏概览", padding=10
        )
        overview.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        for column in range(4):
            overview.columnconfigure(column, weight=1)

        total_card = ttk.Frame(overview, padding=10, relief="ridge")
        total_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(total_card, text="Binance 账户总金额").pack(anchor="w")
        self.account_total_label = ttk.Label(
            total_card,
            textvariable=self.account_total_var,
            style="AccountValue.TLabel",
        )
        self.account_total_label.pack(anchor="w", pady=(4, 0))
        ttk.Label(total_card, text="全部激活钱包折算为 USDC").pack(anchor="w")

        realized_card = ttk.Frame(overview, padding=10, relief="ridge")
        realized_card.grid(row=0, column=1, sticky="nsew", padx=6)
        ttk.Label(realized_card, text="已实现盈亏金额（程序）").pack(anchor="w")
        self.realized_pnl_label = ttk.Label(
            realized_card,
            textvariable=self.realized_pnl_var,
            style="AccountValue.TLabel",
        )
        self.realized_pnl_label.pack(anchor="w", pady=(4, 0))
        ttk.Label(realized_card, text="已确认成交，包含已记录手续费").pack(anchor="w")

        unrealized_card = ttk.Frame(overview, padding=10, relief="ridge")
        unrealized_card.grid(row=0, column=2, sticky="nsew", padx=6)
        ttk.Label(unrealized_card, text="未实现盈亏金额（程序）").pack(anchor="w")
        self.unrealized_pnl_label = ttk.Label(
            unrealized_card,
            textvariable=self.unrealized_pnl_var,
            style="AccountValue.TLabel",
        )
        self.unrealized_pnl_label.pack(anchor="w", pady=(4, 0))
        ttk.Label(unrealized_card, text="程序持仓按最新买卖中间价估算").pack(anchor="w")

        refresh_card = ttk.Frame(overview, padding=10)
        refresh_card.grid(row=0, column=3, sticky="nsew", padx=(6, 0))
        ttk.Button(
            refresh_card,
            text="立即刷新",
            command=lambda: self._refresh_account_overview(manual=True),
        ).pack(anchor="e")
        ttk.Button(
            refresh_card,
            text="打开运行配置",
            command=lambda: self.notebook.select(self.config_page),
        ).pack(anchor="e", pady=(8, 0))
        ttk.Label(
            overview,
            textvariable=self.account_status_var,
            foreground="#5f6b76",
            wraplength=1100,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        settings = ttk.LabelFrame(
            self.config_page, text="运行配置", padding=14
        )
        settings.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        for column in (1, 3, 5, 7):
            settings.columnconfigure(column, weight=1)

        ttk.Label(settings, text="API 供应商").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            settings,
            textvariable=self.provider_var,
            values=("binance_stocks",),
            state="readonly",
            width=21,
        ).grid(row=0, column=1, sticky="ew", padx=(5, 14))
        ttk.Label(settings, text="量化策略").grid(row=0, column=2, sticky="w")
        ttk.Combobox(
            settings,
            textvariable=self.strategy_var,
            values=("five_minute_breakout",),
            state="readonly",
            width=24,
        ).grid(row=0, column=3, sticky="ew", padx=(5, 14))
        ttk.Label(settings, text="交易模式").grid(row=0, column=4, sticky="w")
        ttk.Combobox(
            settings,
            textvariable=self.mode_var,
            values=("PAPER", "REAL"),
            state="readonly",
            width=10,
        ).grid(row=0, column=5, sticky="ew", padx=(5, 14))
        ttk.Button(settings, text="保存非敏感配置", command=self._save_config).grid(
            row=0, column=6, columnspan=2, sticky="e"
        )

        ttk.Label(settings, text="API Key").grid(row=1, column=0, sticky="w", pady=(9, 0))
        ttk.Entry(settings, textvariable=self.api_key_var).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(5, 14), pady=(9, 0)
        )
        ttk.Label(settings, text="API Secret").grid(
            row=1, column=3, sticky="w", pady=(9, 0)
        )
        ttk.Entry(settings, textvariable=self.api_secret_var, show="●").grid(
            row=1, column=4, columnspan=2, sticky="ew", padx=(5, 14), pady=(9, 0)
        )
        ttk.Button(settings, text="检查 API 与股票", command=self._check_connection).grid(
            row=1, column=6, columnspan=2, sticky="e", pady=(9, 0)
        )

        ttk.Label(settings, text="MA 周期").grid(row=2, column=0, sticky="w", pady=(9, 0))
        ttk.Entry(settings, textvariable=self.ma_var, width=8).grid(
            row=2, column=1, sticky="w", padx=(5, 14), pady=(9, 0)
        )
        ttk.Label(settings, text="买入金额(USDC)").grid(
            row=2, column=2, sticky="w", pady=(9, 0)
        )
        ttk.Entry(settings, textvariable=self.buy_notional_var, width=12).grid(
            row=2, column=3, sticky="w", padx=(5, 14), pady=(9, 0)
        )
        ttk.Label(settings, text="卖出数量").grid(
            row=2, column=4, sticky="w", pady=(9, 0)
        )
        ttk.Entry(settings, textvariable=self.sell_quantity_var, width=12).grid(
            row=2, column=5, sticky="w", padx=(5, 14), pady=(9, 0)
        )
        ttk.Label(settings, text="每日最多交易").grid(
            row=2, column=6, sticky="w", pady=(9, 0)
        )
        ttk.Entry(settings, textvariable=self.max_trades_var, width=8).grid(
            row=2, column=7, sticky="w", padx=(5, 0), pady=(9, 0)
        )

        ttk.Label(settings, text="单笔上限(USDC)").grid(
            row=3, column=0, sticky="w", pady=(9, 0)
        )
        ttk.Entry(settings, textvariable=self.max_order_notional_var, width=12).grid(
            row=3, column=1, sticky="w", padx=(5, 14), pady=(9, 0)
        )
        ttk.Label(settings, text="每日买入上限").grid(
            row=3, column=2, sticky="w", pady=(9, 0)
        )
        ttk.Entry(
            settings, textvariable=self.max_daily_buy_notional_var, width=12
        ).grid(row=3, column=3, sticky="w", padx=(5, 14), pady=(9, 0))
        ttk.Label(settings, text="止损/止盈(%)").grid(
            row=3, column=4, sticky="w", pady=(9, 0)
        )
        risk_frame = ttk.Frame(settings)
        risk_frame.grid(row=3, column=5, sticky="w", padx=(5, 14), pady=(9, 0))
        ttk.Entry(risk_frame, textvariable=self.stop_loss_var, width=6).pack(side=LEFT)
        ttk.Label(risk_frame, text="/").pack(side=LEFT, padx=3)
        ttk.Entry(risk_frame, textvariable=self.take_profit_var, width=6).pack(side=LEFT)
        ttk.Label(settings, text="信号有效期(秒)").grid(
            row=3, column=6, sticky="w", pady=(9, 0)
        )
        ttk.Entry(settings, textvariable=self.max_signal_age_var, width=8).grid(
            row=3, column=7, sticky="w", padx=(5, 0), pady=(9, 0)
        )

        warning = (
            "默认 PAPER 只记录模拟订单。REAL 会真实下单；实盘 SELL 仅用于平掉程序确认的"
            "多头，不会建立空头。未知订单会锁定实盘。API Secret 仅驻留内存且不会保存。"
        )
        ttk.Label(settings, text=warning, foreground="#9a5b00", wraplength=1100).grid(
            row=4, column=0, columnspan=8, sticky="w", pady=(9, 0)
        )

        symbols = ttk.Frame(self.main_page, padding=(10, 5))
        symbols.grid(row=1, column=0, sticky="ew")
        ttk.Label(symbols, text="股票代码").pack(side=LEFT)
        entry = ttk.Entry(symbols, textvariable=self.symbol_var, width=30)
        entry.pack(side=LEFT, padx=6)
        entry.bind("<Return>", lambda _event: self._add_symbols())
        ttk.Button(symbols, text="添加", command=self._add_symbols).pack(side=LEFT)
        ttk.Button(symbols, text="移除所选", command=self._remove_selected).pack(
            side=LEFT, padx=(6, 0)
        )
        ttk.Button(
            symbols,
            text="核对后解除未知订单锁",
            command=self._resolve_unknown_selected,
        ).pack(side=LEFT, padx=(6, 0))
        ttk.Button(
            symbols, text="全部停止(不平仓)", command=self._stop_all
        ).pack(side=RIGHT)
        ttk.Button(symbols, text="全部启动", command=self._start_all).pack(
            side=RIGHT, padx=6
        )
        ttk.Button(symbols, text="停止所选(不平仓)", command=self._stop_selected).pack(
            side=RIGHT
        )
        ttk.Button(symbols, text="启动所选", command=self._start_selected).pack(
            side=RIGHT, padx=6
        )

        table_frame = ttk.Frame(self.main_page, padding=(10, 0))
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = (
            "status",
            "direction",
            "price",
            "ma",
            "warmup",
            "trades",
            "position",
            "entry",
            "pending",
            "daily_notional",
            "message",
        )
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="tree headings",
            selectmode="extended",
        )
        self.tree.heading("#0", text="股票")
        self.tree.column("#0", width=90, minwidth=70, anchor="center")
        headings = {
            "status": "状态",
            "direction": "日线方向",
            "price": "最新价",
            "ma": "MA",
            "warmup": "预热",
            "trades": "今日交易",
            "position": "程序持仓",
            "entry": "持仓均价",
            "pending": "未决订单",
            "daily_notional": "今日买入额",
            "message": "信息",
        }
        widths = {
            "status": 90,
            "direction": 90,
            "price": 100,
            "ma": 100,
            "warmup": 80,
            "trades": 85,
            "position": 90,
            "entry": 90,
            "pending": 80,
            "daily_notional": 95,
            "message": 360,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=70,
                anchor="w" if column == "message" else "center",
            )
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(
            table_frame, orient="horizontal", command=self.tree.xview
        )
        self.tree.configure(
            yscrollcommand=scrollbar.set,
            xscrollcommand=horizontal.set,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("error", foreground="#b00020")
        self.tree.tag_configure("running", foreground="#087830")
        self.tree.tag_configure("signal", foreground="#0856a8")

        ttk.Label(self.main_page, text="运行日志", padding=(10, 7, 10, 2)).grid(
            row=3, column=0, sticky="w"
        )
        log_frame = ttk.Frame(self.main_page, padding=(10, 0, 10, 10))
        log_frame.grid(row=4, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        from tkinter import Text

        self.log = Text(log_frame, height=10, wrap="word", state="disabled")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

    def _current_config(self) -> AppConfig:
        config = AppConfig(
            symbols=list(self.tree.get_children()),
            provider=self.provider_var.get(),
            strategy=self.strategy_var.get(),
            trading_mode=self.mode_var.get(),
            ma_period=int(self.ma_var.get()),
            buy_notional=self.buy_notional_var.get().strip(),
            sell_quantity=self.sell_quantity_var.get().strip(),
            max_trades_per_day=int(self.max_trades_var.get()),
            max_order_notional=self.max_order_notional_var.get().strip(),
            max_daily_buy_notional=self.max_daily_buy_notional_var.get().strip(),
            stop_loss_percent=self.stop_loss_var.get().strip(),
            take_profit_percent=self.take_profit_var.get().strip(),
            max_signal_age_seconds=int(self.max_signal_age_var.get()),
            rest_base_url=self.config.rest_base_url,
            websocket_base_url=self.config.websocket_base_url,
            recv_window=self.config.recv_window,
        )
        config.validate()
        return config

    def _runner_config(self) -> RunnerConfig:
        return RunnerConfig(
            app=self._current_config(),
            api_key=self.api_key_var.get(),
            api_secret=self.api_secret_var.get(),
        )

    def _insert_symbol(self, symbol: str) -> None:
        symbol = symbol.upper()
        if self.tree.exists(symbol):
            return
        self.tree.insert(
            "",
            END,
            iid=symbol,
            text=symbol,
            values=(
                "已停止",
                "UNKNOWN",
                "-",
                "-",
                "0/0",
                "0",
                "0",
                "-",
                "0",
                "0",
                "未启动",
            ),
        )

    def _add_symbols(self) -> None:
        try:
            raw_symbols = re.split(r"[,，;；\s]+", self.symbol_var.get())
            new_symbols = normalize_symbols(raw_symbols)
            combined = set(self.tree.get_children()) | set(new_symbols)
            if len(combined) > MAX_SYMBOLS:
                raise ValueError(f"股票数量不能超过 {MAX_SYMBOLS} 只")
            for symbol in new_symbols:
                self._insert_symbol(symbol)
            self.symbol_var.set("")
        except ValueError as exc:
            messagebox.showerror("股票代码错误", str(exc))

    def _remove_selected(self) -> None:
        for symbol in self.tree.selection():
            self.controller.stop(symbol)
            self.tree.delete(symbol)

    def _resolve_unknown_selected(self) -> None:
        selected = self._selected_symbols()
        if not selected:
            return
        locked = [
            symbol
            for symbol in selected
            if self.controller.unknown_live_orders(symbol) > 0
        ]
        if not locked:
            messagebox.showinfo("没有锁定", "所选股票没有未知实盘订单。")
            return
        confirmed = messagebox.askyesno(
            "确认已经人工核对",
            "只有在你已经登录 Binance，确认所有未知订单的成交状态，"
            "并处理了对应持仓后才能解除。\n\n"
            f"即将解除：{', '.join(locked)}\n\n确认已经完成核对吗？",
            icon="warning",
        )
        if not confirmed:
            return
        try:
            total = sum(
                self.controller.resolve_unknown_live_orders(symbol)
                for symbol in locked
            )
            messagebox.showinfo("已解除", f"已归档 {total} 笔未知订单记录。")
        except RuntimeError as exc:
            messagebox.showerror("无法解除", str(exc))

    def _selected_symbols(self) -> list[str]:
        selected = list(self.tree.selection())
        if not selected:
            messagebox.showinfo("请选择股票", "请先在列表中选择至少一只股票。")
        return selected

    def _start_selected(self) -> None:
        self._start_symbols(self._selected_symbols())

    def _start_all(self) -> None:
        self._start_symbols(list(self.tree.get_children()))

    def _start_symbols(self, symbols: list[str]) -> None:
        if not symbols:
            return
        try:
            config = self._runner_config()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        if config.app.trading_mode == "REAL" and not self._confirm_real_mode():
            return
        for symbol in symbols:
            self.controller.start(symbol, config)

    def _stop_selected(self) -> None:
        for symbol in self._selected_symbols():
            self.controller.stop(symbol)

    def _stop_all(self) -> None:
        self.controller.stop_all()

    def _confirm_real_mode(self) -> bool:
        if not self.api_key_var.get().strip() or not self.api_secret_var.get().strip():
            messagebox.showerror("缺少凭据", "REAL 模式必须填写 API Key 和 API Secret。")
            return False
        confirmed = messagebox.askyesno(
            "确认真实交易",
            "当前为 REAL 模式，策略信号会向 Binance 提交真实 MARKET 订单。\n\n"
            f"股票数：{len(self.tree.get_children())}；单笔买入："
            f"{self.buy_notional_var.get()} USDC；每日账户上限："
            f"{self.max_daily_buy_notional_var.get()} USDC。\n"
            f"止损/止盈：{self.stop_loss_var.get()}% / "
            f"{self.take_profit_var.get()}%。SELL 只会平掉程序确认的多头。\n"
            "账户还必须已经接受 Binance 美股交易免责声明。\n\n确认继续吗？",
            icon="warning",
        )
        return confirmed

    def _save_config(self) -> None:
        try:
            self.config = self._current_config()
            self.store.save(self.config)
            messagebox.showinfo("已保存", f"配置已保存到:\n{self.store.path}")
            self._refresh_account_overview(manual=True)
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("保存失败", str(exc))

    def _check_connection(self) -> None:
        try:
            runner_config = self._runner_config()
        except (ValueError, TypeError) as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        symbols = list(self.tree.selection()) or list(self.tree.get_children())[:1]
        if not symbols:
            messagebox.showinfo("请添加股票", "请先添加至少一只股票。")
            return
        symbol = symbols[0]
        self._append_log("INFO", symbol, "正在检查 Binance API 与股票代码")

        def check() -> None:
            try:
                provider = create_provider(runner_config)
                info = provider.check_symbol(symbol)
                validation = info.get("validation", "")
                message = validation or (
                    f"连接成功；{symbol} tradability={info.get('tradability', 'UNKNOWN')}"
                )
                self._enqueue_event(("dialog", "info", "连接检查", message))
                self._enqueue_event(("log", "INFO", symbol, message))
                self._enqueue_event(("account_refresh",))
            except Exception as exc:
                self._enqueue_event(("dialog", "error", "连接失败", str(exc)))
                self._enqueue_event(("log", "ERROR", symbol, str(exc)))

        threading.Thread(target=check, name="api-check", daemon=True).start()

    def _account_runner_config(self) -> RunnerConfig:
        account_config = replace(
            self.config,
            trading_mode=self.mode_var.get().strip().upper(),
        )
        account_config.validate()
        return RunnerConfig(
            app=account_config,
            api_key=self.api_key_var.get().strip(),
            api_secret=self.api_secret_var.get().strip(),
        )

    def _account_refresh_tick(self) -> None:
        if self._closed:
            return
        self._refresh_account_overview()
        self.root.after(ACCOUNT_REFRESH_MS, self._account_refresh_tick)

    def _refresh_account_overview(self, manual: bool = False) -> None:
        if self._account_refresh_inflight:
            if manual:
                self.account_status_var.set("账户数据正在刷新，请稍候")
            return
        try:
            runner_config = self._account_runner_config()
        except (TypeError, ValueError) as exc:
            self.account_status_var.set(f"账户概览配置无效：{exc}")
            return
        paper = runner_config.app.trading_mode != "REAL"
        prices = dict(self._latest_prices)
        if not runner_config.api_key or not runner_config.api_secret:
            performance = self.controller.portfolio_performance(
                paper=paper,
                market_prices=prices,
            )
            ledger_name = "模拟" if paper else "实盘"
            message = (
                "请在“运行配置”页填写 API Key 和 Secret，"
                f"才能查询 Binance 账户总金额；盈亏来自{ledger_name}订单账本"
            )
            self._apply_account_overview(
                AccountOverview(
                    total_balance=None,
                    realized_pnl=performance.realized_pnl,
                    unrealized_pnl=performance.unrealized_pnl,
                    missing_price_symbols=performance.missing_price_symbols,
                    message=message,
                    updated_at=int(time.time() * 1000),
                )
            )
            return

        self._account_refresh_inflight = True
        self.account_status_var.set("正在刷新 Binance 钱包余额和程序盈亏…")

        def refresh() -> None:
            total_balance: Decimal | None = None
            errors: list[str] = []
            try:
                provider = create_provider(runner_config)
                try:
                    total_balance = provider.get_account_total("USDC")
                except Exception as exc:
                    errors.append(f"账户总金额不可用：{exc}")
                for symbol in self.controller.open_position_symbols(paper=paper):
                    try:
                        prices[symbol] = provider.get_latest_price(symbol)
                    except Exception as exc:
                        prices.pop(symbol, None)
                        errors.append(f"{symbol} 报价不可用：{exc}")
                performance = self.controller.portfolio_performance(
                    paper=paper,
                    market_prices=prices,
                )
                message = (
                    "；".join(errors)
                    if errors
                    else (
                        "账户总金额来自 Binance 全部激活钱包的 USDC 折算；"
                        f"盈亏仅统计本程序{'模拟' if paper else '实盘'}订单"
                    )
                )
                overview = AccountOverview(
                    total_balance=total_balance,
                    realized_pnl=performance.realized_pnl,
                    unrealized_pnl=performance.unrealized_pnl,
                    missing_price_symbols=performance.missing_price_symbols,
                    message=message,
                    updated_at=int(time.time() * 1000),
                )
            except Exception as exc:
                performance = self.controller.portfolio_performance(
                    paper=paper,
                    market_prices=prices,
                )
                overview = AccountOverview(
                    total_balance=None,
                    realized_pnl=performance.realized_pnl,
                    unrealized_pnl=performance.unrealized_pnl,
                    missing_price_symbols=performance.missing_price_symbols,
                    message=f"账户概览刷新失败：{exc}",
                    updated_at=int(time.time() * 1000),
                )
            self._enqueue_event(("account", overview))

        threading.Thread(
            target=refresh,
            name="account-overview-refresh",
            daemon=True,
        ).start()

    def _apply_account_overview(self, overview: AccountOverview) -> None:
        self._account_refresh_inflight = False
        self.account_total_var.set(
            "不可用"
            if overview.total_balance is None
            else f"{overview.total_balance:,.2f} {overview.currency}"
        )
        self._set_pnl_value(
            self.realized_pnl_var,
            self.realized_pnl_label,
            overview.realized_pnl,
            overview.currency,
        )
        self._set_pnl_value(
            self.unrealized_pnl_var,
            self.unrealized_pnl_label,
            overview.unrealized_pnl,
            overview.currency,
        )
        detail = overview.message
        if overview.missing_price_symbols:
            detail += "；缺少持仓报价：" + ", ".join(
                overview.missing_price_symbols
            )
        timestamp = datetime.fromtimestamp(
            overview.updated_at / 1000
        ).strftime("%H:%M:%S")
        self.account_status_var.set(f"{detail}；更新时间 {timestamp}")

    @staticmethod
    def _set_pnl_value(
        variable: StringVar,
        label: ttk.Label,
        value: Decimal | None,
        currency: str,
    ) -> None:
        if value is None:
            variable.set("行情不可用")
            label.configure(style="AccountValue.TLabel")
            return
        variable.set(f"{value:+,.2f} {currency}")
        if value > 0:
            label.configure(style="AccountPositive.TLabel")
        elif value < 0:
            label.configure(style="AccountNegative.TLabel")
        else:
            label.configure(style="AccountValue.TLabel")

    def _enqueue_event(self, event: tuple) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(event)
            except queue.Full:
                pass

    def _drain_events(self) -> None:
        for _ in range(200):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "snapshot":
                self._apply_snapshot(event[1])
            elif event[0] == "log":
                self._append_log(event[1], event[2], event[3])
            elif event[0] == "dialog":
                _kind, severity, title, message = event
                if severity == "error":
                    messagebox.showerror(title, message)
                else:
                    messagebox.showinfo(title, message)
            elif event[0] == "account":
                self._apply_account_overview(event[1])
            elif event[0] == "account_refresh":
                self._refresh_account_overview(manual=True)
        delay = 10 if not self.events.empty() else 100
        self.root.after(delay, self._drain_events)

    def _apply_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        if not self.tree.exists(snapshot.symbol):
            return
        if snapshot.last_price is not None and snapshot.last_price > 0:
            self._latest_prices[snapshot.symbol] = snapshot.last_price
        tag = ""
        if snapshot.state is RunState.ERROR:
            tag = "error"
        elif snapshot.state is RunState.RUNNING:
            tag = "running"
        elif snapshot.state is RunState.SIGNAL:
            tag = "signal"
        values = (
            STATE_TEXT[snapshot.state],
            snapshot.direction.value,
            self._format_decimal(snapshot.last_price),
            self._format_decimal(snapshot.ma_value),
            f"{snapshot.warmup_bars}/{snapshot.warmup_required}",
            str(snapshot.trades_today),
            self._format_decimal(snapshot.position_quantity),
            self._format_decimal(snapshot.average_entry_price),
            str(snapshot.pending_orders),
            self._format_decimal(snapshot.daily_buy_notional),
            snapshot.message,
        )
        self.tree.item(snapshot.symbol, values=values, tags=(tag,) if tag else ())

    def _append_log(self, level: str, symbol: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert(END, f"[{timestamp}] [{level}] [{symbol}] {message}\n")
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 5000:
            self.log.delete("1.0", f"{line_count - 5000 + 1}.0")
        self.log.see(END)
        self.log.configure(state="disabled")

    @staticmethod
    def _format_decimal(value: object | None) -> str:
        if value is None:
            return "-"
        return format(value, "f")

    def _on_close(self) -> None:
        self._closed = True
        self.controller.stop_all()
        self.controller.join_all(timeout_per_runner=0.5)
        self.root.destroy()


def main() -> None:
    root = Tk()
    AutoQuantApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

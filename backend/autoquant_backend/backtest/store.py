from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from autoquant_backend.backtest.models import (
    BacktestTrade,
    DAY_MS,
    DOWNLOAD_DAYS,
    SYMBOL_RE,
    _decimal,
)
from autoquant_shared.config import AppConfig, strategy_config_snapshot
from autoquant_shared.models import Bar

class BacktestStore:
    """SQLite persistence for historical candles and reproducible backtests."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._status_condition = threading.Condition()
        self._status_revision = 0
        self._initialize()

    def _notify_status_changed(self) -> None:
        with self._status_condition:
            self._status_revision += 1
            self._status_condition.notify_all()

    def wait_for_status_change(
        self, after_revision: int, timeout: float
    ) -> int:
        normalized_revision = max(-1, int(after_revision))
        normalized_timeout = min(max(float(timeout), 0.0), 10.0)
        with self._status_condition:
            if normalized_revision == self._status_revision:
                self._status_condition.wait_for(
                    lambda: self._status_revision != normalized_revision,
                    timeout=normalized_timeout,
                )
            return self._status_revision

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS market_bars (
                    provider TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time INTEGER NOT NULL,
                    close_time INTEGER NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT NOT NULL DEFAULT '0',
                    downloaded_at INTEGER NOT NULL,
                    PRIMARY KEY (provider, symbol, interval, open_time)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_market_bars_range
                ON market_bars(provider, symbol, interval, open_time)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS market_downloads (
                    download_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    days INTEGER NOT NULL,
                    start_time INTEGER NOT NULL,
                    end_time INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    current_interval TEXT NOT NULL DEFAULT '',
                    progress INTEGER NOT NULL DEFAULT 0,
                    daily_count INTEGER NOT NULL DEFAULT 0,
                    five_minute_count INTEGER NOT NULL DEFAULT 0,
                    one_minute_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    completed_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            download_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(market_downloads)"
                ).fetchall()
            }
            if "updated_at" not in download_columns:
                connection.execute(
                    "ALTER TABLE market_downloads ADD COLUMN "
                    "updated_at INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "UPDATE market_downloads SET updated_at = created_at "
                    "WHERE updated_at = 0"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_market_downloads_time
                ON market_downloads(created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS backtest_runs (
                    run_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    download_id TEXT NOT NULL DEFAULT '',
                    strategy TEXT NOT NULL,
                    start_time INTEGER NOT NULL,
                    end_time INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    strategy_config_json TEXT NOT NULL DEFAULT '{}',
                    trade_count INTEGER NOT NULL DEFAULT 0,
                    win_count INTEGER NOT NULL DEFAULT 0,
                    loss_count INTEGER NOT NULL DEFAULT 0,
                    total_pnl TEXT NOT NULL DEFAULT '0',
                    return_percent TEXT NOT NULL DEFAULT '0',
                    max_drawdown_percent TEXT NOT NULL DEFAULT '0',
                    message TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    completed_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            run_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(backtest_runs)"
                ).fetchall()
            }
            if "strategy_config_json" not in run_columns:
                connection.execute(
                    "ALTER TABLE backtest_runs ADD COLUMN "
                    "strategy_config_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "download_id" not in run_columns:
                connection.execute(
                    "ALTER TABLE backtest_runs ADD COLUMN "
                    "download_id TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_backtest_runs_time
                ON backtest_runs(created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS backtest_trades (
                    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_time INTEGER NOT NULL,
                    exit_time INTEGER NOT NULL,
                    entry_price TEXT NOT NULL,
                    exit_price TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    pnl TEXT NOT NULL,
                    exit_reason TEXT NOT NULL,
                    signal_reason TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_backtest_trades_run
                ON backtest_trades(run_id, entry_time)
                """
            )
            now = int(time.time() * 1000)
            connection.execute(
                """
                UPDATE market_downloads
                SET status='FAILED', message='后端重启，下载任务已中断',
                    updated_at=?, completed_at=?
                WHERE status IN ('QUEUED', 'RUNNING')
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE backtest_runs
                SET status='FAILED', message='后端重启，回测任务已中断',
                    completed_at=?
                WHERE status IN ('QUEUED', 'RUNNING', 'STOPPING')
                """,
                (now,),
            )

    def create_download(
        self, provider: str, symbol: str, start_time: int, end_time: int
    ) -> str:
        download_id = uuid.uuid4().hex
        now = int(time.time() * 1000)
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO market_downloads (
                    download_id, provider, symbol, days, start_time, end_time,
                    status, message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', '等待下载', ?, ?)
                """,
                (
                    download_id,
                    provider,
                    symbol,
                    DOWNLOAD_DAYS,
                    start_time,
                    end_time,
                    now,
                    now,
                ),
            )
        self._notify_status_changed()
        return download_id

    def update_download(self, download_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "current_interval",
            "progress",
            "daily_count",
            "five_minute_count",
            "one_minute_count",
            "message",
            "completed_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = int(time.time() * 1000)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                f"UPDATE market_downloads SET {assignments} WHERE download_id = ?",
                (*updates.values(), download_id),
            )
        self._notify_status_changed()

    def upsert_bars(self, provider: str, bars: list[Bar]) -> int:
        if not bars:
            return 0
        now = int(time.time() * 1000)
        rows = [
            (
                provider,
                bar.symbol.upper(),
                bar.interval,
                bar.open_time,
                bar.close_time,
                _decimal(bar.open),
                _decimal(bar.high),
                _decimal(bar.low),
                _decimal(bar.close),
                _decimal(bar.volume),
                now,
            )
            for bar in bars
            if bar.closed
        ]
        with self._lock, closing(self._connect()) as connection, connection:
            connection.executemany(
                """
                INSERT INTO market_bars (
                    provider, symbol, interval, open_time, close_time,
                    open, high, low, close, volume, downloaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, symbol, interval, open_time) DO UPDATE SET
                    close_time=excluded.close_time,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    downloaded_at=excluded.downloaded_at
                """,
                rows,
            )
        return len(rows)

    def import_bars(self, provider: str, bars: Iterable[Bar]) -> int:
        """Atomically import a validated stream of bars into the durable store."""
        now = int(time.time() * 1000)
        imported = 0
        batch: list[tuple[Any, ...]] = []

        def flush(connection: sqlite3.Connection) -> None:
            nonlocal imported
            if not batch:
                return
            connection.executemany(
                """
                INSERT INTO market_bars (
                    provider, symbol, interval, open_time, close_time,
                    open, high, low, close, volume, downloaded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, symbol, interval, open_time) DO UPDATE SET
                    close_time=excluded.close_time,
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    downloaded_at=excluded.downloaded_at
                """,
                batch,
            )
            imported += len(batch)
            batch.clear()

        with self._lock, closing(self._connect()) as connection, connection:
            for bar in bars:
                batch.append(
                    (
                        provider,
                        bar.symbol.upper(),
                        bar.interval,
                        bar.open_time,
                        bar.close_time,
                        _decimal(bar.open),
                        _decimal(bar.high),
                        _decimal(bar.low),
                        _decimal(bar.close),
                        _decimal(bar.volume),
                        now,
                    )
                )
                if len(batch) >= 5000:
                    flush(connection)
            flush(connection)
        return imported

    def count_bars(
        self,
        provider: str,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
    ) -> int:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM market_bars
                WHERE provider = ? AND symbol = ? AND interval = ?
                  AND open_time >= ? AND close_time <= ?
                """,
                (provider, symbol, interval, start_time, end_time),
            ).fetchone()
        return int(row["count"] if row else 0)

    def latest_bar_open_time(
        self,
        provider: str,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
    ) -> int | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT MAX(open_time) AS open_time FROM market_bars
                WHERE provider = ? AND symbol = ? AND interval = ?
                  AND open_time >= ? AND open_time <= ?
                """,
                (provider, symbol, interval, start_time, end_time),
            ).fetchone()
        if row is None or row["open_time"] is None:
            return None
        return int(row["open_time"])

    def load_bars(
        self,
        provider: str,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
    ) -> list[Bar]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM market_bars
                WHERE provider = ? AND symbol = ? AND interval = ?
                  AND open_time >= ? AND close_time <= ?
                ORDER BY open_time
                """,
                (provider, symbol, interval, start_time, end_time),
            ).fetchall()
        return [
            Bar(
                symbol=str(row["symbol"]),
                interval=str(row["interval"]),
                open_time=int(row["open_time"]),
                close_time=int(row["close_time"]),
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])),
                closed=True,
            )
            for row in rows
        ]

    def list_downloads(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM market_downloads ORDER BY created_at DESC LIMIT ?",
                (min(max(int(limit), 1), 200),),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_complete_download(
        self, provider: str, symbol: str
    ) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM market_downloads
                WHERE provider = ? AND symbol = ? AND status = 'COMPLETED'
                ORDER BY completed_at DESC LIMIT 1
                """,
                (provider, symbol),
            ).fetchone()
        return dict(row) if row else None

    def get_download(self, download_id: str) -> dict[str, Any] | None:
        normalized = download_id.strip()
        if not normalized:
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM market_downloads WHERE download_id = ?",
                (normalized,),
            ).fetchone()
        return dict(row) if row else None

    def has_active_download(self, provider: str, symbol: str) -> bool:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM market_downloads
                WHERE provider = ? AND symbol = ?
                  AND status IN ('QUEUED', 'RUNNING') LIMIT 1
                """,
                (provider, symbol),
            ).fetchone()
        return row is not None

    def record_import(
        self,
        provider: str,
        symbol: str,
        start_time: int,
        end_time: int,
        counts: dict[str, int],
    ) -> str:
        download_id = uuid.uuid4().hex
        now = int(time.time() * 1000)
        days = max(1, (end_time - start_time + DAY_MS) // DAY_MS)
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO market_downloads (
                    download_id, provider, symbol, days, start_time, end_time,
                    status, progress, daily_count, five_minute_count,
                    one_minute_count, message, created_at, updated_at,
                    completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'COMPLETED', 100, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    download_id,
                    provider,
                    symbol,
                    int(days),
                    start_time,
                    end_time,
                    counts.get("1d", 0),
                    counts.get("5m", 0),
                    counts.get("1m", 0),
                    "历史 K 线数据包导入完成",
                    now,
                    now,
                    now,
                ),
            )
        self._notify_status_changed()
        return download_id

    def delete_historical_bars(
        self, provider: str, symbol: str
    ) -> dict[str, int]:
        provider = provider.strip().lower()
        symbol = symbol.strip().upper()
        if provider not in {"binance_stocks", "binance_futures"}:
            raise ValueError("历史 K 线行情源不受支持")
        if SYMBOL_RE.fullmatch(symbol) is None:
            raise ValueError("历史 K 线标的格式不正确")
        with self._lock, closing(self._connect()) as connection, connection:
            active = connection.execute(
                """
                SELECT 1 FROM market_downloads
                WHERE provider = ? AND symbol = ?
                  AND status IN ('QUEUED', 'RUNNING') LIMIT 1
                """,
                (provider, symbol),
            ).fetchone()
            if active is not None:
                raise RuntimeError(f"{symbol} 的历史 K 线仍在下载，暂不能删除")
            bars_cursor = connection.execute(
                "DELETE FROM market_bars WHERE provider = ? AND symbol = ?",
                (provider, symbol),
            )
            downloads_cursor = connection.execute(
                "DELETE FROM market_downloads WHERE provider = ? AND symbol = ?",
                (provider, symbol),
            )
        if downloads_cursor.rowcount:
            self._notify_status_changed()
        return {
            "deleted_bars": max(0, int(bars_cursor.rowcount)),
            "deleted_downloads": max(0, int(downloads_cursor.rowcount)),
        }

    def create_run(
        self,
        provider: str,
        symbol: str,
        strategy: str,
        start_time: int,
        end_time: int,
        config: AppConfig,
        download_id: str = "",
    ) -> str:
        run_id = uuid.uuid4().hex
        now = int(time.time() * 1000)
        config_payload = asdict(config)
        for secret_field in (
            "api_key",
            "api_secret",
            "openai_api_key",
            "deepseek_api_key",
            "qwen_api_key",
        ):
            config_payload[secret_field] = ""
        config_json = json.dumps(
            config_payload, ensure_ascii=False, sort_keys=True
        )
        strategy_config_json = json.dumps(
            strategy_config_snapshot(config, strategy),
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO backtest_runs (
                    run_id, provider, symbol, download_id, strategy,
                    start_time, end_time,
                    status, config_json, strategy_config_json, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, '等待回测', ?)
                """,
                (
                    run_id,
                    provider,
                    symbol,
                    download_id.strip(),
                    strategy,
                    start_time,
                    end_time,
                    config_json,
                    strategy_config_json,
                    now,
                ),
            )
        self._notify_status_changed()
        return run_id

    def has_active_run_for_download(self, download_id: str) -> bool:
        normalized = download_id.strip()
        if not normalized:
            return False
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM backtest_runs
                WHERE download_id = ?
                  AND status IN ('QUEUED', 'RUNNING', 'STOPPING')
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return row is not None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        normalized = run_id.strip()
        if not normalized:
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM backtest_runs WHERE run_id = ?",
                (normalized,),
            ).fetchone()
        return dict(row) if row else None

    def request_run_cancel(self, run_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE backtest_runs
                SET status='STOPPING', message='正在停止回测', completed_at=0
                WHERE run_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (run_id.strip(),),
            )
        changed = cursor.rowcount > 0
        if changed:
            self._notify_status_changed()
        return changed

    def complete_run(
        self,
        run_id: str,
        trades: list[BacktestTrade],
        *,
        total_pnl: Decimal,
        return_percent: Decimal,
        max_drawdown_percent: Decimal,
    ) -> bool:
        wins = sum(1 for trade in trades if trade.pnl > 0)
        losses = sum(1 for trade in trades if trade.pnl < 0)
        now = int(time.time() * 1000)
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT status FROM backtest_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or str(row["status"]) not in {"QUEUED", "RUNNING"}:
                return False
            connection.executemany(
                """
                INSERT INTO backtest_trades (
                    run_id, side, entry_time, exit_time, entry_price,
                    exit_price, quantity, pnl, exit_reason, signal_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        trade.side,
                        trade.entry_time,
                        trade.exit_time,
                        _decimal(trade.entry_price),
                        _decimal(trade.exit_price),
                        _decimal(trade.quantity),
                        _decimal(trade.pnl),
                        trade.exit_reason,
                        trade.signal_reason,
                    )
                    for trade in trades
                ],
            )
            connection.execute(
                """
                UPDATE backtest_runs SET
                    status='COMPLETED', trade_count=?, win_count=?, loss_count=?,
                    total_pnl=?, return_percent=?, max_drawdown_percent=?,
                    message='回测完成', completed_at=?
                WHERE run_id=?
                """,
                (
                    len(trades),
                    wins,
                    losses,
                    _decimal(total_pnl),
                    _decimal(return_percent),
                    _decimal(max_drawdown_percent),
                    now,
                    run_id,
                ),
            )
        self._notify_status_changed()
        return True

    def update_run(self, run_id: str, status: str, message: str) -> None:
        completed_at = (
            int(time.time() * 1000) if status in {"FAILED", "COMPLETED"} else 0
        )
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE backtest_runs
                SET status=?, message=?, completed_at=? WHERE run_id=?
                """,
                (status, message, completed_at, run_id),
            )
        self._notify_status_changed()

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT run_id, provider, symbol, strategy, start_time, end_time,
                       download_id,
                       status, trade_count, win_count, loss_count, total_pnl,
                       return_percent, max_drawdown_percent, message,
                       created_at, completed_at, strategy_config_json
                FROM backtest_runs ORDER BY created_at DESC LIMIT ?
                """,
                (min(max(int(limit), 1), 500),),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_snapshot = item.pop("strategy_config_json", "{}")
            try:
                snapshot = json.loads(str(raw_snapshot))
            except (json.JSONDecodeError, TypeError, ValueError):
                snapshot = {}
            item["strategy_config"] = (
                snapshot if isinstance(snapshot, dict) else {}
            )
            results.append(item)
        return results

    def backtest_trades(
        self, run_id: str, limit: int = 50_000
    ) -> list[dict[str, Any]]:
        normalized = run_id.strip()
        if not normalized:
            raise ValueError("回测记录 ID 不能为空")
        with self._lock, closing(self._connect()) as connection:
            run = connection.execute(
                "SELECT 1 FROM backtest_runs WHERE run_id = ?",
                (normalized,),
            ).fetchone()
            if run is None:
                raise ValueError("回测记录不存在")
            rows = connection.execute(
                """
                SELECT trade_id, run_id, side, entry_time, exit_time,
                       entry_price, exit_price, quantity, pnl,
                       exit_reason, signal_reason
                FROM backtest_trades
                WHERE run_id = ?
                ORDER BY entry_time, trade_id
                LIMIT ?
                """,
                (normalized, min(max(int(limit), 1), 50_000)),
            ).fetchall()
        return [dict(row) for row in rows]




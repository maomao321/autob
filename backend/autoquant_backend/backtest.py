from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from autoquant_backend.providers.base import TradingProvider
from autoquant_backend.strategies.five_minute_breakout import (
    FiveMinuteBreakoutStrategy,
)
from autoquant_shared.config import AppConfig, strategy_config_snapshot
from autoquant_shared.models import Bar, Side


DAY_MS = 86_400_000
DOWNLOAD_DAYS = 180
INTERVAL_MS = {"1d": DAY_MS, "5m": 300_000, "1m": 60_000}
DOWNLOAD_INTERVALS = ("1d", "5m", "1m")
ARCHIVE_VERSION = 1
ARCHIVE_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
BAR_CSV_FIELDS = (
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
ARCHIVE_FILES = {interval: f"bars_{interval}.csv" for interval in DOWNLOAD_INTERVALS}
SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.-]{0,19}", re.ASCII)


def _decimal(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    side: str
    entry_time: int
    exit_time: int
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    pnl: Decimal
    exit_reason: str
    signal_reason: str


class BacktestStore:
    """SQLite persistence for historical candles and reproducible backtests."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

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
                    completed_at INTEGER NOT NULL DEFAULT 0
                )
                """
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
                    completed_at=?
                WHERE status IN ('QUEUED', 'RUNNING')
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE backtest_runs
                SET status='FAILED', message='后端重启，回测任务已中断',
                    completed_at=?
                WHERE status IN ('QUEUED', 'RUNNING')
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
                    status, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', '等待下载', ?)
                """,
                (
                    download_id,
                    provider,
                    symbol,
                    DOWNLOAD_DAYS,
                    start_time,
                    end_time,
                    now,
                ),
            )
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
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                f"UPDATE market_downloads SET {assignments} WHERE download_id = ?",
                (*updates.values(), download_id),
            )

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
                    one_minute_count, message, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'COMPLETED', 100, ?, ?, ?, ?, ?, ?)
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
                ),
            )
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
                    run_id, provider, symbol, strategy, start_time, end_time,
                    status, config_json, strategy_config_json, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, '等待回测', ?)
                """,
                (
                    run_id,
                    provider,
                    symbol,
                    strategy,
                    start_time,
                    end_time,
                    config_json,
                    strategy_config_json,
                    now,
                ),
            )
        return run_id

    def complete_run(
        self,
        run_id: str,
        trades: list[BacktestTrade],
        *,
        total_pnl: Decimal,
        return_percent: Decimal,
        max_drawdown_percent: Decimal,
    ) -> None:
        wins = sum(1 for trade in trades if trade.pnl > 0)
        losses = sum(1 for trade in trades if trade.pnl < 0)
        now = int(time.time() * 1000)
        with self._lock, closing(self._connect()) as connection, connection:
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

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT run_id, provider, symbol, strategy, start_time, end_time,
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


class HistoricalArchiveService:
    """Export and import portable, validated per-symbol historical datasets."""

    def __init__(self, store: BacktestStore) -> None:
        self.store = store

    def export(self, provider: str, symbol: str) -> tuple[bytes, str]:
        provider = provider.strip().lower()
        symbol = self._symbol(symbol)
        counts: dict[str, int] = {}
        ranges: dict[str, dict[str, int]] = {}
        with self.store._lock, closing(self.store._connect()) as connection:
            for interval in DOWNLOAD_INTERVALS:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count, MIN(open_time) AS start_time,
                           MAX(close_time) AS end_time
                    FROM market_bars
                    WHERE provider = ? AND symbol = ? AND interval = ?
                    """,
                    (provider, symbol, interval),
                ).fetchone()
                count = int(row["count"] if row else 0)
                counts[interval] = count
                ranges[interval] = {
                    "start_time": int(row["start_time"] or 0) if row else 0,
                    "end_time": int(row["end_time"] or 0) if row else 0,
                }
            if not any(counts.values()):
                raise ValueError(f"{symbol} 没有可导出的持久化历史 K 线")

            manifest = {
                "format": "autoquant-historical-klines",
                "version": ARCHIVE_VERSION,
                "provider": provider,
                "symbol": symbol,
                "exported_at": int(time.time() * 1000),
                "counts": counts,
                "ranges": ranges,
            }
            output = io.BytesIO()
            with zipfile.ZipFile(
                output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                )
                for interval in DOWNLOAD_INTERVALS:
                    with archive.open(ARCHIVE_FILES[interval], "w") as raw:
                        text = io.TextIOWrapper(
                            raw, encoding="utf-8", newline="", write_through=True
                        )
                        writer = csv.writer(text, lineterminator="\n")
                        writer.writerow(BAR_CSV_FIELDS)
                        cursor = connection.execute(
                            """
                            SELECT open_time, close_time, open, high, low, close,
                                   volume
                            FROM market_bars
                            WHERE provider = ? AND symbol = ? AND interval = ?
                            ORDER BY open_time
                            """,
                            (provider, symbol, interval),
                        )
                        while True:
                            rows = cursor.fetchmany(5000)
                            if not rows:
                                break
                            writer.writerows(tuple(row[field] for field in BAR_CSV_FIELDS) for row in rows)
                        text.detach()
        filename = f"{symbol}_{provider}_historical_klines.zip"
        return output.getvalue(), filename

    def import_archive(
        self, payload: bytes, *, expected_symbol: str = ""
    ) -> dict[str, Any]:
        if not payload:
            raise ValueError("导入文件为空")
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload), mode="r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValueError("导入文件不是有效的 AutoQuant K 线 ZIP 数据包") from exc
        with archive:
            infos = archive.infolist()
            names = set(archive.namelist())
            if len(names) != len(infos):
                raise ValueError("K 线数据包包含重复文件名")
            if any(info.flag_bits & 0x1 for info in infos):
                raise ValueError("K 线数据包不能包含加密文件")
            required = {"manifest.json", *ARCHIVE_FILES.values()}
            if not required.issubset(names):
                missing = sorted(required - names)
                raise ValueError("K 线数据包缺少文件: " + ", ".join(missing))
            if any(
                name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/")
                for name in names
            ):
                raise ValueError("K 线数据包包含不安全的文件路径")
            total_size = sum(info.file_size for info in infos)
            if total_size > ARCHIVE_MAX_UNCOMPRESSED_BYTES:
                raise ValueError("K 线数据包解压后超过 512 MB 限制")
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > 1_000_000:
                raise ValueError("K 线数据包清单超过 1 MB 限制")
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
                raise ValueError("K 线数据包清单无效") from exc
            if not isinstance(manifest, dict):
                raise ValueError("K 线数据包清单必须是 JSON 对象")
            if manifest.get("format") != "autoquant-historical-klines":
                raise ValueError("不支持的 K 线数据包格式")
            if int(manifest.get("version", 0)) != ARCHIVE_VERSION:
                raise ValueError("不支持的 K 线数据包版本")
            provider = str(manifest.get("provider", "")).strip().lower()
            if provider not in {"binance_stocks", "binance_futures"}:
                raise ValueError("K 线数据包行情源不受支持")
            symbol = self._symbol(str(manifest.get("symbol", "")))
            selected_symbol = expected_symbol.strip().upper()
            if selected_symbol and self._symbol(selected_symbol) != symbol:
                raise ValueError(
                    f"数据包标的 {symbol} 与页面指定标的 {selected_symbol} 不一致"
                )
            raw_counts = manifest.get("counts", {})
            if not isinstance(raw_counts, dict):
                raise ValueError("K 线数据包 counts 清单无效")
            expected_counts = {
                interval: self._nonnegative_int(raw_counts.get(interval, 0), "K 线数量")
                for interval in DOWNLOAD_INTERVALS
            }
            counts = {interval: 0 for interval in DOWNLOAD_INTERVALS}
            range_state: dict[str, int | None] = {"start": None, "end": 0}

            def bars() -> Iterator[Bar]:
                for interval in DOWNLOAD_INTERVALS:
                    previous_open_time = -1
                    with archive.open(ARCHIVE_FILES[interval], "r") as raw:
                        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                        reader = csv.DictReader(text)
                        if tuple(reader.fieldnames or ()) != BAR_CSV_FIELDS:
                            raise ValueError(f"{interval} CSV 表头不正确")
                        for row_number, row in enumerate(reader, start=2):
                            try:
                                bar = self._parse_bar(symbol, interval, row)
                            except (ArithmeticError, TypeError, ValueError) as exc:
                                raise ValueError(
                                    f"{interval} CSV 第 {row_number} 行无效: {exc}"
                                ) from exc
                            if bar.open_time <= previous_open_time:
                                raise ValueError(
                                    f"{interval} CSV 第 {row_number} 行时间重复或未递增"
                                )
                            previous_open_time = bar.open_time
                            counts[interval] += 1
                            if range_state["start"] is None:
                                range_state["start"] = bar.open_time
                            else:
                                range_state["start"] = min(
                                    range_state["start"], bar.open_time
                                )
                            range_state["end"] = max(
                                int(range_state["end"] or 0), bar.close_time
                            )
                            yield bar
                    if counts[interval] != expected_counts[interval]:
                        raise ValueError(
                            f"{interval} CSV 实际 {counts[interval]} 根，"
                            f"与清单 {expected_counts[interval]} 根不一致"
                        )

            imported = self.store.import_bars(provider, bars())
            if (
                imported <= 0
                or range_state["start"] is None
                or int(range_state["end"] or 0) <= range_state["start"]
            ):
                raise ValueError("K 线数据包没有可导入的历史数据")
            download_id = self.store.record_import(
                provider,
                symbol,
                range_state["start"],
                int(range_state["end"] or 0),
                counts,
            )
        return {
            "download_id": download_id,
            "provider": provider,
            "symbol": symbol,
            "counts": counts,
            "imported_rows": imported,
            "start_time": range_state["start"],
            "end_time": int(range_state["end"] or 0),
        }

    @staticmethod
    def _symbol(value: str) -> str:
        symbol = value.strip().upper()
        if SYMBOL_RE.fullmatch(symbol) is None:
            raise ValueError("历史 K 线标的格式不正确")
        return symbol

    @staticmethod
    def _nonnegative_int(value: Any, label: str) -> int:
        parsed = int(value)
        if parsed < 0:
            raise ValueError(f"{label}不能为负数")
        return parsed

    @staticmethod
    def _parse_bar(
        symbol: str, interval: str, row: dict[str, Any]
    ) -> Bar:
        if None in row:
            raise ValueError("CSV 包含未声明的额外列")
        open_time = int(row["open_time"])
        close_time = int(row["close_time"])
        prices = {
            field: Decimal(str(row[field]))
            for field in ("open", "high", "low", "close", "volume")
        }
        if open_time < 0 or close_time <= open_time:
            raise ValueError("K 线时间范围不正确")
        if any(not value.is_finite() for value in prices.values()):
            raise ValueError("OHLCV 必须是有限数字")
        if any(prices[field] <= 0 for field in ("open", "high", "low", "close")):
            raise ValueError("OHLC 必须为正数")
        if prices["volume"] < 0:
            raise ValueError("成交量不能为负数")
        if prices["high"] < max(prices["open"], prices["close"], prices["low"]):
            raise ValueError("最高价小于其他价格")
        if prices["low"] > min(prices["open"], prices["close"], prices["high"]):
            raise ValueError("最低价大于其他价格")
        return Bar(
            symbol=symbol,
            interval=interval,
            open_time=open_time,
            close_time=close_time,
            open=prices["open"],
            high=prices["high"],
            low=prices["low"],
            close=prices["close"],
            volume=prices["volume"],
            closed=True,
        )


class HistoricalDownloader:
    def __init__(
        self,
        store: BacktestStore,
        provider_factory: Callable[[], TradingProvider],
        provider_name: str,
    ) -> None:
        self.store = store
        self.provider_factory = provider_factory
        self.provider_name = provider_name

    def start(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("回测标的不能为空")
        if self.provider_name != "binance_futures":
            raise ValueError("180 天 1 分钟历史下载当前仅支持 Binance Futures")
        if self.store.has_active_download(self.provider_name, normalized):
            raise ValueError(f"{normalized} 已有历史 K 线下载任务在执行")
        end_time = int(time.time() * 1000) - 1
        start_time = end_time - DOWNLOAD_DAYS * DAY_MS
        download_id = self.store.create_download(
            self.provider_name, normalized, start_time, end_time
        )
        threading.Thread(
            target=self._run,
            args=(download_id, normalized, start_time, end_time),
            name=f"backtest-download-{normalized}",
            daemon=True,
        ).start()
        return download_id

    def _run(
        self,
        download_id: str,
        symbol: str,
        start_time: int,
        end_time: int,
    ) -> None:
        try:
            provider = self.provider_factory()
            counts: dict[str, int] = {}
            self.store.update_download(
                download_id, status="RUNNING", message="正在下载历史 K 线"
            )
            for index, interval in enumerate(DOWNLOAD_INTERVALS):
                count = self._download_interval(
                    download_id,
                    provider,
                    symbol,
                    interval,
                    start_time,
                    end_time,
                    index,
                )
                if count <= 0:
                    raise RuntimeError(f"{symbol} 未下载到 {interval} K 线")
                counts[interval] = count
            self.store.update_download(
                download_id,
                status="COMPLETED",
                current_interval="",
                progress=100,
                daily_count=counts["1d"],
                five_minute_count=counts["5m"],
                one_minute_count=counts["1m"],
                message=(
                    "最近 180 天范围内可用的日线、5 分钟和 1 分钟 K 线"
                    "下载完成，数量以行情源实际返回为准"
                ),
                completed_at=int(time.time() * 1000),
            )
        except Exception as exc:
            self.store.update_download(
                download_id,
                status="FAILED",
                message=str(exc) or exc.__class__.__name__,
                completed_at=int(time.time() * 1000),
            )

    def _download_interval(
        self,
        download_id: str,
        provider: TradingProvider,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        interval_index: int,
    ) -> int:
        step = INTERVAL_MS[interval]
        latest_open_time = self.store.latest_bar_open_time(
            self.provider_name, symbol, interval, start_time, end_time
        )
        cursor = (
            start_time
            if latest_open_time is None
            else max(start_time, latest_open_time + step)
        )
        pages = 0
        while cursor <= end_time:
            bars = provider.get_historical_bars(
                symbol, interval, cursor, end_time, 1500
            )
            eligible = [
                bar
                for bar in bars
                if bar.closed and cursor <= bar.open_time <= end_time
            ]
            if not eligible:
                break
            eligible.sort(key=lambda bar: bar.open_time)
            self.store.upsert_bars(self.provider_name, eligible)
            next_cursor = eligible[-1].open_time + step
            if next_cursor <= cursor:
                raise RuntimeError(f"{interval} 历史行情分页未向前推进")
            cursor = next_cursor
            pages += 1
            completed_fraction = min(
                1.0, max(0.0, (cursor - start_time) / (end_time - start_time))
            )
            progress = int((interval_index + completed_fraction) / 3 * 100)
            count = self.store.count_bars(
                self.provider_name, symbol, interval, start_time, end_time
            )
            field = {
                "1d": "daily_count",
                "5m": "five_minute_count",
                "1m": "one_minute_count",
            }[interval]
            self.store.update_download(
                download_id,
                current_interval=interval,
                progress=min(progress, 99),
                message=f"正在下载 {interval} K 线，已持久化 {count} 根",
                **{field: count},
            )
            if len(eligible) < 1500:
                break
            if pages > 1000:
                raise RuntimeError(f"{interval} 历史行情分页次数异常")
        return self.store.count_bars(
            self.provider_name, symbol, interval, start_time, end_time
        )


class BacktestService:
    def __init__(self, store: BacktestStore) -> None:
        self.store = store

    def start(
        self, provider: str, symbol: str, strategy: str, config: AppConfig
    ) -> str:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("回测标的不能为空")
        if strategy != "five_minute_breakout":
            raise ValueError(f"暂不支持回测策略: {strategy}")
        download = self.store.latest_complete_download(provider, normalized)
        if download is None:
            raise ValueError("请先完成该标的历史 K 线下载或导入")
        run_id = self.store.create_run(
            provider,
            normalized,
            strategy,
            int(download["start_time"]),
            int(download["end_time"]),
            config,
        )
        threading.Thread(
            target=self._run,
            args=(run_id, provider, normalized, download, config),
            name=f"backtest-run-{normalized}",
            daemon=True,
        ).start()
        return run_id

    def _run(
        self,
        run_id: str,
        provider: str,
        symbol: str,
        download: dict[str, Any],
        config: AppConfig,
    ) -> None:
        try:
            self.store.update_run(run_id, "RUNNING", "正在执行回测")
            start_time = int(download["start_time"])
            end_time = int(download["end_time"])
            daily = self.store.load_bars(
                provider, symbol, "1d", start_time, end_time
            )
            five = self.store.load_bars(
                provider, symbol, "5m", start_time, end_time
            )
            minute = self.store.load_bars(
                provider, symbol, "1m", start_time, end_time
            )
            trades = self._simulate(symbol, daily, five, minute, config)
            total_pnl = sum((trade.pnl for trade in trades), Decimal("0"))
            capital = Decimal(config.buy_notional)
            return_percent = (
                total_pnl / capital * Decimal("100")
                if capital > 0
                else Decimal("0")
            )
            equity = capital
            peak = capital
            max_drawdown = Decimal("0")
            for trade in trades:
                equity += trade.pnl
                peak = max(peak, equity)
                if peak > 0:
                    max_drawdown = max(
                        max_drawdown, (peak - equity) / peak * Decimal("100")
                    )
            self.store.complete_run(
                run_id,
                trades,
                total_pnl=total_pnl,
                return_percent=return_percent,
                max_drawdown_percent=max_drawdown,
            )
        except Exception as exc:
            self.store.update_run(
                run_id, "FAILED", str(exc) or exc.__class__.__name__
            )

    @staticmethod
    def _simulate(
        symbol: str,
        daily: list[Bar],
        five: list[Bar],
        minute: list[Bar],
        config: AppConfig,
    ) -> list[BacktestTrade]:
        strategy = FiveMinuteBreakoutStrategy(
            symbol=symbol,
            manual_direction=None,
            entry_context_bars=config.ai_entry_timing_bars,
        )
        stop_fraction = Decimal(config.stop_loss_percent) / Decimal("100")
        take_fraction = Decimal(config.take_profit_percent) / Decimal("100")
        notional = Decimal(config.buy_notional)
        trades: list[BacktestTrade] = []
        position: dict[str, Any] | None = None
        day_index = -1
        minute_index = 0

        def close_position(bar: Bar, price: Decimal, reason: str) -> None:
            nonlocal position
            if position is None:
                return
            multiplier = Decimal("1") if position["side"] == "LONG" else Decimal("-1")
            pnl = (price - position["entry_price"]) * position["quantity"] * multiplier
            trades.append(
                BacktestTrade(
                    side=position["side"],
                    entry_time=position["entry_time"],
                    exit_time=bar.close_time,
                    entry_price=position["entry_price"],
                    exit_price=price,
                    quantity=position["quantity"],
                    pnl=pnl,
                    exit_reason=reason,
                    signal_reason=position["signal_reason"],
                )
            )
            position = None

        for five_bar in five:
            while minute_index < len(minute) and minute[minute_index].close_time <= five_bar.close_time:
                minute_bar = minute[minute_index]
                minute_index += 1
                if position is None or minute_bar.open_time <= position["entry_time"]:
                    continue
                if position["side"] == "LONG":
                    stop_price = position["entry_price"] * (Decimal("1") - stop_fraction)
                    take_price = position["entry_price"] * (Decimal("1") + take_fraction)
                    if minute_bar.low <= stop_price:
                        close_position(minute_bar, stop_price, "STOP_LOSS")
                    elif minute_bar.high >= take_price:
                        close_position(minute_bar, take_price, "TAKE_PROFIT")
                else:
                    stop_price = position["entry_price"] * (Decimal("1") + stop_fraction)
                    take_price = position["entry_price"] * (Decimal("1") - take_fraction)
                    if minute_bar.high >= stop_price:
                        close_position(minute_bar, stop_price, "STOP_LOSS")
                    elif minute_bar.low <= take_price:
                        close_position(minute_bar, take_price, "TAKE_PROFIT")

            while day_index + 1 < len(daily) and daily[day_index + 1].open_time <= five_bar.open_time:
                next_day = daily[day_index + 1]
                if next_day.close_time < five_bar.open_time:
                    day_index += 1
                    continue
                day_index += 1
                strategy.on_bar(next_day)
                strategy.seed_daily_history(daily[:day_index])
                break
            if day_index < 0 or not (
                daily[day_index].open_time <= five_bar.open_time <= daily[day_index].close_time
            ):
                continue
            signal = strategy.on_bar(five_bar)
            if signal is None:
                continue
            signal_side = "LONG" if signal.side is Side.BUY else "SHORT"
            added_quantity = notional / signal.price
            if position is None:
                position = {
                    "side": signal_side,
                    "entry_time": five_bar.close_time,
                    "entry_price": signal.price,
                    "quantity": added_quantity,
                    "additions": 0,
                    "signal_reason": signal.reason,
                }
                continue
            if (
                position["side"] != signal_side
                or position["additions"]
                >= config.max_additions_per_position
            ):
                continue
            previous_cost = position["entry_price"] * position["quantity"]
            position["quantity"] += added_quantity
            position["entry_price"] = (
                previous_cost + signal.price * added_quantity
            ) / position["quantity"]
            position["additions"] += 1
            position["signal_reason"] += f"；第 {position['additions']} 次加仓：{signal.reason}"

        if position is not None:
            final_bar = minute[-1] if minute else five[-1]
            close_position(final_bar, final_bar.close, "END_OF_DATA")
        return trades


__all__ = [
    "BacktestService",
    "BacktestStore",
    "BacktestTrade",
    "HistoricalArchiveService",
    "HistoricalDownloader",
]

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from autoquant.config import default_config_path
from autoquant.models import OrderRequest


CONSUMED_STATUSES = {
    "SUBMITTING",
    "UNKNOWN",
    "ACKNOWLEDGED",
    "NEW",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELED",
    "EXPIRED",
}
RECONCILABLE_STATUSES = {
    "ACKNOWLEDGED",
    "NEW",
    "ACCEPTED",
    "PARTIALLY_FILLED",
}


def default_state_path() -> Path:
    return default_config_path().with_name("orders.sqlite3")


@dataclass(frozen=True, slots=True)
class OrderRecord:
    client_order_id: str
    symbol: str
    side: str
    trading_day: int
    status: str
    order_id: str
    paper: bool
    created_at: int
    updated_at: int
    message: str


class OrderLedger:
    """A durable order ledger used to enforce limits across restarts."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_state_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    trading_day INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    order_id TEXT NOT NULL DEFAULT '',
                    paper INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    message TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_symbol_day "
                "ON orders(symbol, trading_day)"
            )

    def record_submitting(
        self,
        order: OrderRequest,
        trading_day: int,
        *,
        paper: bool,
    ) -> None:
        if not order.client_order_id:
            raise ValueError("client_order_id 不能为空")
        now = int(time.time() * 1000)
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO orders (
                    client_order_id, symbol, side, trading_day, status,
                    order_id, paper, created_at, updated_at, message
                ) VALUES (?, ?, ?, ?, 'SUBMITTING', '', ?, ?, ?, '')
                """,
                (
                    order.client_order_id,
                    order.symbol,
                    order.side.value,
                    trading_day,
                    int(paper),
                    now,
                    now,
                ),
            )

    def mark_acknowledged(
        self,
        client_order_id: str,
        order_id: str,
        message: str = "",
    ) -> None:
        self._update(client_order_id, "ACKNOWLEDGED", order_id, message)

    def mark_rejected(self, client_order_id: str, message: str = "") -> None:
        self._update(client_order_id, "REJECTED", None, message)

    def mark_unknown(self, client_order_id: str, message: str = "") -> None:
        self._update(client_order_id, "UNKNOWN", None, message)

    def mark_lifecycle(
        self,
        client_order_id: str,
        status: str,
        message: str = "",
    ) -> None:
        normalized = status.strip().upper()
        if normalized:
            self._update(client_order_id, normalized, None, message)

    def _update(
        self,
        client_order_id: str,
        status: str,
        order_id: str | None,
        message: str,
    ) -> None:
        now = int(time.time() * 1000)
        with self._lock, closing(self._connect()) as connection, connection:
            if order_id is None:
                connection.execute(
                    """
                    UPDATE orders
                    SET status = ?, updated_at = ?, message = ?
                    WHERE client_order_id = ?
                    """,
                    (status, now, message, client_order_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE orders
                    SET status = ?, order_id = ?, updated_at = ?, message = ?
                    WHERE client_order_id = ?
                    """,
                    (status, order_id, now, message, client_order_id),
                )

    def mark_stale_submitting_unknown(self, symbol: str) -> int:
        now = int(time.time() * 1000)
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE orders
                SET status = 'UNKNOWN', updated_at = ?,
                    message = CASE
                        WHEN message = '' THEN '程序在订单提交期间退出，成交状态未知'
                        ELSE message
                    END
                WHERE symbol = ? AND status = 'SUBMITTING'
                """,
                (now, symbol),
            )
            return cursor.rowcount

    def count_consumed(self, symbol: str, trading_day: int) -> int:
        statuses = sorted(CONSUMED_STATUSES)
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM orders
                WHERE symbol = ? AND trading_day = ?
                  AND status IN ({placeholders})
                """,
                (symbol, trading_day, *statuses),
            ).fetchone()
        return int(row["total"])

    def unresolved_with_order_id(self, symbol: str) -> list[OrderRecord]:
        statuses = sorted(RECONCILABLE_STATUSES)
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f"""
                SELECT * FROM orders
                WHERE symbol = ? AND paper = 0 AND order_id != ''
                  AND status IN ({placeholders})
                ORDER BY created_at
                """,
                (symbol, *statuses),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def unknown_count(self, symbol: str) -> int:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM orders "
                "WHERE symbol = ? AND status = 'UNKNOWN'",
                (symbol,),
            ).fetchone()
        return int(row["total"])

    @staticmethod
    def _to_record(row: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=row["side"],
            trading_day=row["trading_day"],
            status=row["status"],
            order_id=row["order_id"],
            paper=bool(row["paper"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message=row["message"],
        )

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from autoquant.config import default_config_path
from autoquant.models import OrderRequest, Side


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
    requested_notional: Decimal
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal


@dataclass(frozen=True, slots=True)
class PositionSummary:
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")


class RiskLimitError(RuntimeError):
    """An atomic account-level risk reservation was rejected."""


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
                    message TEXT NOT NULL DEFAULT '',
                    requested_notional TEXT NOT NULL DEFAULT '0',
                    requested_quantity TEXT NOT NULL DEFAULT '0',
                    filled_quantity TEXT NOT NULL DEFAULT '0',
                    average_price TEXT NOT NULL DEFAULT '0'
                )
                """
            )
            existing_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(orders)")
            }
            added_tracking_columns = False
            for name in (
                "requested_notional",
                "requested_quantity",
                "filled_quantity",
                "average_price",
            ):
                if name not in existing_columns:
                    added_tracking_columns = True
                    connection.execute(
                        f"ALTER TABLE orders ADD COLUMN {name} "
                        "TEXT NOT NULL DEFAULT '0'"
                    )
            if added_tracking_columns:
                connection.execute(
                    """
                    UPDATE orders
                    SET status = 'UNKNOWN',
                        message = '旧版本未保存成交数量，必须人工核对'
                    WHERE paper = 0
                      AND status IN (
                          'FILLED', 'PARTIALLY_FILLED', 'CANCELED', 'EXPIRED'
                      )
                    """
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_symbol_day "
                "ON orders(symbol, trading_day)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_symbol_status "
                "ON orders(symbol, status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_daily_risk "
                "ON orders(trading_day, paper, side, status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_position "
                "ON orders(symbol, paper, created_at)"
            )

    def record_submitting(
        self,
        order: OrderRequest,
        trading_day: int,
        *,
        paper: bool,
        max_daily_buy_notional: Decimal | None = None,
    ) -> None:
        if not order.client_order_id:
            raise ValueError("client_order_id 不能为空")
        now = int(time.time() * 1000)
        with self._lock, closing(self._connect()) as connection, connection:
            # Serialize the limit check and reservation across independent app
            # processes. A deferred transaction would allow two processes to
            # both observe the same remaining allowance before either inserts.
            connection.execute("BEGIN IMMEDIATE")
            requested_notional = (
                order.buy_notional if order.side is Side.BUY else Decimal("0")
            )
            blocking_statuses = sorted(
                RECONCILABLE_STATUSES | {"SUBMITTING", "UNKNOWN"}
            )
            placeholders = ",".join("?" for _ in blocking_statuses)
            blocking = connection.execute(
                f"""
                SELECT status FROM orders
                WHERE symbol = ? AND paper = ?
                  AND status IN ({placeholders})
                LIMIT 1
                """,
                (order.symbol, int(paper), *blocking_statuses),
            ).fetchone()
            if blocking is not None:
                raise RiskLimitError(
                    f"{order.symbol} 仍有 {blocking['status']} 订单，禁止重复下单"
                )
            if not paper:
                rows = connection.execute(
                    """
                    SELECT side, filled_quantity, average_price
                    FROM orders
                    WHERE symbol = ? AND paper = 0
                      AND CAST(filled_quantity AS REAL) > 0
                    ORDER BY created_at, client_order_id
                    """,
                    (order.symbol,),
                ).fetchall()
                position = self._position_summary_from_rows(rows)
                if order.side is Side.BUY and position.quantity > 0:
                    raise RiskLimitError(
                        f"{order.symbol} 已有程序持仓，禁止重复买入"
                    )
                if order.side is Side.SELL:
                    if position.quantity <= 0:
                        raise RiskLimitError(
                            f"{order.symbol} 没有程序持仓，禁止卖空"
                        )
                    if order.sell_quantity > position.quantity:
                        raise RiskLimitError(
                            f"{order.symbol} 卖出数量超过程序持仓"
                        )
            if max_daily_buy_notional is not None and requested_notional > 0:
                rows = connection.execute(
                    """
                    SELECT requested_notional FROM orders
                    WHERE trading_day = ? AND paper = ? AND side = 'BUY'
                      AND status != 'REJECTED'
                      AND status != 'MANUALLY_RESOLVED'
                    """,
                    (trading_day, int(paper)),
                ).fetchall()
                reserved = sum(
                    (Decimal(row["requested_notional"]) for row in rows),
                    Decimal("0"),
                )
                if reserved + requested_notional > max_daily_buy_notional:
                    raise RiskLimitError(
                        f"账户当日买入金额 {reserved + requested_notional} "
                        f"将超过上限 {max_daily_buy_notional}"
                    )
            connection.execute(
                """
                INSERT INTO orders (
                    client_order_id, symbol, side, trading_day, status,
                    order_id, paper, created_at, updated_at, message,
                    requested_notional, requested_quantity,
                    filled_quantity, average_price
                ) VALUES (?, ?, ?, ?, 'SUBMITTING', '', ?, ?, ?, '', ?, ?, '0', '0')
                """,
                (
                    order.client_order_id,
                    order.symbol,
                    order.side.value,
                    trading_day,
                    int(paper),
                    now,
                    now,
                    str(requested_notional),
                    str(order.sell_quantity if order.side is Side.SELL else 0),
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
        filled_quantity: Decimal | None = None,
        average_price: Decimal | None = None,
    ) -> None:
        normalized = status.strip().upper()
        if normalized:
            self._update(
                client_order_id,
                normalized,
                None,
                message,
                filled_quantity,
                average_price,
            )

    def _update(
        self,
        client_order_id: str,
        status: str,
        order_id: str | None,
        message: str,
        filled_quantity: Decimal | None = None,
        average_price: Decimal | None = None,
    ) -> None:
        now = int(time.time() * 1000)
        with self._lock, closing(self._connect()) as connection, connection:
            if filled_quantity is not None:
                connection.execute(
                    """
                    UPDATE orders
                    SET status = ?, updated_at = ?, message = ?,
                        filled_quantity = ?, average_price = ?
                    WHERE client_order_id = ?
                    """,
                    (
                        status,
                        now,
                        message,
                        str(filled_quantity),
                        str(average_price or 0),
                        client_order_id,
                    ),
                )
            elif order_id is None:
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

    def get_record(self, client_order_id: str) -> OrderRecord | None:
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return self._to_record(row) if row is not None else None

    def unknown_count(self, symbol: str, paper: bool | None = None) -> int:
        query = (
            "SELECT COUNT(*) AS total FROM orders "
            "WHERE symbol = ? AND status = 'UNKNOWN'"
        )
        params: tuple[object, ...] = (symbol,)
        if paper is not None:
            query += " AND paper = ?"
            params = (symbol, int(paper))
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(query, params).fetchone()
        return int(row["total"])

    def resolve_unknown(self, symbol: str, *, paper: bool = False) -> int:
        now = int(time.time() * 1000)
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE orders
                SET status = 'MANUALLY_RESOLVED', updated_at = ?,
                    message = '用户确认已在 Binance 核对并处理'
                WHERE symbol = ? AND paper = ? AND status = 'UNKNOWN'
                """,
                (now, symbol, int(paper)),
            )
            return cursor.rowcount

    def pending_count(self, symbol: str, *, paper: bool) -> int:
        statuses = sorted(RECONCILABLE_STATUSES | {"SUBMITTING"})
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, closing(self._connect()) as connection, connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total FROM orders
                WHERE symbol = ? AND paper = ?
                  AND status IN ({placeholders})
                """,
                (symbol, int(paper), *statuses),
            ).fetchone()
        return int(row["total"])

    def daily_buy_notional(self, trading_day: int, *, paper: bool) -> Decimal:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT requested_notional FROM orders
                WHERE trading_day = ? AND paper = ? AND side = 'BUY'
                  AND status != 'REJECTED'
                  AND status != 'MANUALLY_RESOLVED'
                """,
                (trading_day, int(paper)),
            ).fetchall()
        return sum(
            (Decimal(row["requested_notional"]) for row in rows), Decimal("0")
        )

    def position_summary(self, symbol: str, *, paper: bool) -> PositionSummary:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT side, filled_quantity, average_price
                FROM orders
                WHERE symbol = ? AND paper = ?
                  AND CAST(filled_quantity AS REAL) > 0
                ORDER BY created_at, client_order_id
                """,
                (symbol, int(paper)),
            ).fetchall()
        return self._position_summary_from_rows(rows)

    @staticmethod
    def _position_summary_from_rows(
        rows: list[sqlite3.Row],
    ) -> PositionSummary:
        quantity = Decimal("0")
        cost = Decimal("0")
        for row in rows:
            filled = Decimal(row["filled_quantity"])
            price = Decimal(row["average_price"])
            if row["side"] == Side.BUY.value:
                quantity += filled
                cost += filled * price
                continue
            if quantity <= 0:
                continue
            sold = min(filled, quantity)
            average = cost / quantity if quantity else Decimal("0")
            quantity -= sold
            cost -= sold * average
        average_price = cost / quantity if quantity > 0 else Decimal("0")
        return PositionSummary(quantity=quantity, average_price=average_price)

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
            requested_notional=Decimal(row["requested_notional"]),
            requested_quantity=Decimal(row["requested_quantity"]),
            filled_quantity=Decimal(row["filled_quantity"]),
            average_price=Decimal(row["average_price"]),
        )

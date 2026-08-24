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
    fee: Decimal
    reduce_only: bool


@dataclass(frozen=True, slots=True)
class PositionSummary:
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class PortfolioPerformance:
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal | None = Decimal("0")
    missing_price_symbols: tuple[str, ...] = ()


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
                    average_price TEXT NOT NULL DEFAULT '0',
                    fee TEXT NOT NULL DEFAULT '0',
                    reduce_only INTEGER NOT NULL DEFAULT 0
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
            if "fee" not in existing_columns:
                connection.execute(
                    "ALTER TABLE orders ADD COLUMN fee "
                    "TEXT NOT NULL DEFAULT '0'"
                )
            if "reduce_only" not in existing_columns:
                connection.execute(
                    "ALTER TABLE orders ADD COLUMN reduce_only "
                    "INTEGER NOT NULL DEFAULT 0"
                )
                # Older versions only supported long positions, so every
                # historical SELL was a long-position reduction.
                connection.execute(
                    "UPDATE orders SET reduce_only = 1 WHERE side = 'SELL'"
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
                Decimal("0") if order.reduce_only else order.buy_notional
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
            rows = connection.execute(
                """
                SELECT side, filled_quantity, average_price, reduce_only
                FROM orders
                WHERE symbol = ? AND paper = ?
                  AND CAST(filled_quantity AS REAL) > 0
                ORDER BY created_at, client_order_id
                """,
                (order.symbol, int(paper)),
            ).fetchall()
            position = self._position_summary_from_rows(rows)
            quantity = position.quantity
            if order.reduce_only:
                if quantity == 0:
                    raise RiskLimitError(
                        f"{order.symbol} 没有程序持仓，不能提交减仓单"
                    )
                expected_side = Side.SELL if quantity > 0 else Side.BUY
                if order.side is not expected_side:
                    raise RiskLimitError(
                        f"{order.symbol} 当前为"
                        f"{'多头' if quantity > 0 else '空头'}持仓，"
                        f"减仓方向必须是 {expected_side.value}"
                    )
                if order.sell_quantity > abs(quantity):
                    raise RiskLimitError(
                        f"{order.symbol} 减仓数量超过程序持仓"
                    )
            else:
                if quantity != 0:
                    raise RiskLimitError(
                        f"{order.symbol} 已有程序"
                        f"{'多头' if quantity > 0 else '空头'}持仓，禁止双向或重复开仓"
                    )
                if order.side is Side.SELL and not order.allow_short:
                    raise RiskLimitError(
                        f"{order.symbol} 当前供应商不允许建立空头"
                    )
            if max_daily_buy_notional is not None and requested_notional > 0:
                rows = connection.execute(
                    """
                    SELECT requested_notional FROM orders
                    WHERE trading_day = ? AND paper = ? AND reduce_only = 0
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
                        f"账户当日开仓金额 {reserved + requested_notional} "
                        f"将超过上限 {max_daily_buy_notional}"
                    )
            connection.execute(
                """
                INSERT INTO orders (
                    client_order_id, symbol, side, trading_day, status,
                    order_id, paper, created_at, updated_at, message,
                    requested_notional, requested_quantity,
                    filled_quantity, average_price, reduce_only
                ) VALUES (?, ?, ?, ?, 'SUBMITTING', '', ?, ?, ?, '', ?, ?, '0', '0', ?)
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
                    str(order.sell_quantity if order.reduce_only else 0),
                    int(order.reduce_only),
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
        fee: Decimal | None = None,
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
                fee,
            )

    def _update(
        self,
        client_order_id: str,
        status: str,
        order_id: str | None,
        message: str,
        filled_quantity: Decimal | None = None,
        average_price: Decimal | None = None,
        fee: Decimal | None = None,
    ) -> None:
        now = int(time.time() * 1000)
        with self._lock, closing(self._connect()) as connection, connection:
            if filled_quantity is not None:
                connection.execute(
                    """
                    UPDATE orders
                    SET status = ?, updated_at = ?, message = ?,
                        filled_quantity = ?, average_price = ?, fee = ?
                    WHERE client_order_id = ?
                    """,
                    (
                        status,
                        now,
                        message,
                        str(filled_quantity),
                        str(average_price or 0),
                        str(fee or 0),
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
                  AND reduce_only = 0
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

    def list_filled_records(self, paper: bool | None = None) -> list[OrderRecord]:
        query = (
            "SELECT * FROM orders "
            "WHERE CAST(filled_quantity AS REAL) > 0"
        )
        params: tuple[object, ...] = ()
        if paper is not None:
            query += " AND paper = ?"
            params = (int(paper),)
        query += " ORDER BY created_at, client_order_id"
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(query, params).fetchall()
        return [self._to_record(row) for row in rows]

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
                WHERE trading_day = ? AND paper = ? AND reduce_only = 0
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
                SELECT side, filled_quantity, average_price, reduce_only
                FROM orders
                WHERE symbol = ? AND paper = ?
                  AND CAST(filled_quantity AS REAL) > 0
                ORDER BY created_at, client_order_id
                """,
                (symbol, int(paper)),
            ).fetchall()
        return self._position_summary_from_rows(rows)

    def open_position_symbols(self, *, paper: bool) -> list[str]:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT symbol, side, filled_quantity, average_price, reduce_only
                FROM orders
                WHERE paper = ? AND CAST(filled_quantity AS REAL) > 0
                ORDER BY symbol, created_at, client_order_id
                """,
                (int(paper),),
            ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["symbol"], []).append(row)
        return sorted(
            symbol
            for symbol, symbol_rows in grouped.items()
            if self._position_summary_from_rows(symbol_rows).quantity != 0
        )

    def portfolio_performance(
        self,
        *,
        paper: bool,
        market_prices: dict[str, Decimal],
    ) -> PortfolioPerformance:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT symbol, side, filled_quantity, average_price, fee,
                       reduce_only
                FROM orders
                WHERE paper = ? AND CAST(filled_quantity AS REAL) > 0
                ORDER BY created_at, client_order_id
                """,
                (int(paper),),
            ).fetchall()
        positions: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
        realized = Decimal("0")
        for row in rows:
            symbol = str(row["symbol"])
            quantity, basis, open_fees = positions.get(
                symbol, (Decimal("0"), Decimal("0"), Decimal("0"))
            )
            filled = Decimal(row["filled_quantity"])
            price = Decimal(row["average_price"])
            fee = Decimal(row["fee"])
            reduce_only = bool(row["reduce_only"])
            delta = filled if row["side"] == Side.BUY.value else -filled
            if not reduce_only and quantity == 0:
                quantity = delta
                basis = filled * price
                open_fees = fee
            elif not reduce_only and quantity * delta > 0:
                quantity += delta
                basis += filled * price
                open_fees += fee
            elif reduce_only and quantity * delta < 0:
                before = abs(quantity)
                closed = min(filled, before)
                average_entry = basis / before if before else Decimal("0")
                allocated_open_fee = (
                    open_fees * closed / before if before else Decimal("0")
                )
                allocated_close_fee = (
                    fee * closed / filled if filled else Decimal("0")
                )
                if quantity > 0:
                    realized += (
                        (price - average_entry) * closed
                        - allocated_open_fee
                        - allocated_close_fee
                    )
                    quantity -= closed
                else:
                    realized += (
                        (average_entry - price) * closed
                        - allocated_open_fee
                        - allocated_close_fee
                    )
                    quantity += closed
                basis -= average_entry * closed
                open_fees -= allocated_open_fee
                if quantity == 0:
                    basis = Decimal("0")
                    open_fees = Decimal("0")
            positions[symbol] = (quantity, basis, open_fees)

        missing: list[str] = []
        unrealized = Decimal("0")
        for symbol, (quantity, basis, open_fees) in positions.items():
            if quantity == 0:
                continue
            price = market_prices.get(symbol)
            if price is None or not price.is_finite() or price <= 0:
                missing.append(symbol)
                continue
            average_entry = basis / abs(quantity)
            if quantity > 0:
                unrealized += (
                    (price - average_entry) * quantity - open_fees
                )
            else:
                unrealized += (
                    (average_entry - price) * abs(quantity) - open_fees
                )
        return PortfolioPerformance(
            realized_pnl=realized,
            unrealized_pnl=None if missing else unrealized,
            missing_price_symbols=tuple(sorted(missing)),
        )

    @staticmethod
    def _position_summary_from_rows(
        rows: list[sqlite3.Row],
    ) -> PositionSummary:
        quantity = Decimal("0")
        basis = Decimal("0")
        for row in rows:
            filled = Decimal(row["filled_quantity"])
            price = Decimal(row["average_price"])
            reduce_only = bool(row["reduce_only"])
            delta = filled if row["side"] == Side.BUY.value else -filled
            if not reduce_only and quantity == 0:
                quantity = delta
                basis = filled * price
                continue
            if not reduce_only and quantity * delta > 0:
                quantity += delta
                basis += filled * price
                continue
            if not reduce_only or quantity * delta >= 0:
                continue
            before = abs(quantity)
            reduced = min(filled, before)
            average = basis / before if before else Decimal("0")
            quantity += reduced if quantity < 0 else -reduced
            basis -= reduced * average
            if quantity == 0:
                basis = Decimal("0")
        average_price = (
            basis / abs(quantity) if quantity != 0 else Decimal("0")
        )
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
            fee=Decimal(row["fee"]),
            reduce_only=bool(row["reduce_only"]),
        )

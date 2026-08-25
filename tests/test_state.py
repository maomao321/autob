from __future__ import annotations

import tempfile
import threading
import unittest
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from autoquant_shared.models import OrderRequest, Side
from autoquant_backend.state import OrderLedger, RiskLimitError


def order(
    client_order_id: str = "aq-test",
    *,
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    buy_notional: str = "100",
    sell_quantity: str = "1",
    reduce_only: bool | None = None,
    allow_short: bool = False,
) -> OrderRequest:
    if reduce_only is None:
        reduce_only = side is Side.SELL
    return OrderRequest(
        symbol=symbol,
        side=side,
        reference_price=Decimal("180"),
        buy_notional=Decimal(buy_notional),
        sell_quantity=Decimal(sell_quantity),
        client_order_id=client_order_id,
        reduce_only=reduce_only,
        allow_short=allow_short,
    )


class OrderLedgerTests(unittest.TestCase):
    def test_old_order_ledger_migrates_and_backfills_close_profit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE orders (
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
                connection.executemany(
                    """
                    INSERT INTO orders (
                        client_order_id, symbol, side, trading_day, status,
                        paper, created_at, updated_at, filled_quantity,
                        average_price
                    ) VALUES (?, 'AAPL', ?, 123, 'FILLED', 1, ?, ?, '2', ?)
                    """,
                    (
                        ("old-open", "BUY", 1, 1, "100"),
                        ("old-close", "SELL", 2, 2, "110"),
                    ),
                )

            migrated = OrderLedger(path)
            closes = migrated.trade_history(action="CLOSE", paper=True)

        self.assertEqual(1, len(closes))
        self.assertEqual(Decimal("20"), closes[0].profit)

    def test_trade_history_persists_and_supports_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite3"
            ledger = OrderLedger(path)
            ledger.record_submitting(
                order("aq-open", buy_notional="200", reduce_only=False),
                123,
                paper=True,
            )
            ledger.mark_lifecycle(
                "aq-open",
                "FILLED",
                filled_quantity=Decimal("2"),
                average_price=Decimal("100"),
                fee=Decimal("1"),
            )
            ledger.record_submitting(
                order(
                    "aq-close",
                    side=Side.SELL,
                    buy_notional="0",
                    sell_quantity="2",
                    reduce_only=True,
                ),
                123,
                paper=True,
            )
            ledger.mark_lifecycle(
                "aq-close",
                "FILLED",
                filled_quantity=Decimal("2"),
                average_price=Decimal("110"),
                fee=Decimal("1"),
                realized_pnl=Decimal("18"),
            )

            restarted = OrderLedger(path)
            records = restarted.trade_history(
                symbol="aapl",
                action="CLOSE",
                paper=True,
            )

        self.assertEqual(1, len(records))
        self.assertEqual("CLOSE", records[0].action)
        self.assertEqual("LONG", records[0].opening_direction)
        self.assertEqual(Decimal("110"), records[0].price)
        self.assertEqual(Decimal("2"), records[0].quantity)
        self.assertEqual(Decimal("220"), records[0].amount)
        self.assertEqual(Decimal("1"), records[0].fee)
        self.assertEqual(Decimal("18"), records[0].profit)

    def test_submitting_order_survives_restart_and_consumes_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite3"
            first = OrderLedger(path)
            first.record_submitting(order(), 123, paper=False)

            restarted = OrderLedger(path)
            changed = restarted.mark_stale_submitting_unknown("AAPL")

            self.assertEqual(1, changed)
            self.assertEqual(1, restarted.count_consumed("AAPL", 123))
            self.assertEqual(1, restarted.unknown_count("AAPL"))

    def test_rejected_order_releases_daily_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(order(), 123, paper=False)
            ledger.mark_rejected("aq-test", "invalid")

            self.assertEqual(0, ledger.count_consumed("AAPL", 123))

    def test_acknowledged_live_order_can_be_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(order(), 123, paper=False)
            ledger.mark_acknowledged("aq-test", "exchange-1")

            records = ledger.unresolved_with_order_id("AAPL")

            self.assertEqual(1, len(records))
            self.assertEqual("exchange-1", records[0].order_id)

    def test_daily_buy_limit_is_shared_across_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(
                order("aq-aapl", buy_notional="100"),
                123,
                paper=False,
                max_daily_buy_notional=Decimal("150"),
            )

            with self.assertRaises(RiskLimitError):
                ledger.record_submitting(
                    order("aq-nvda", symbol="NVDA", buy_notional="60"),
                    123,
                    paper=False,
                    max_daily_buy_notional=Decimal("150"),
                )

    def test_daily_buy_limit_is_atomic_across_ledger_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite3"
            ledgers = (OrderLedger(path), OrderLedger(path))
            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            outcomes_lock = threading.Lock()

            def reserve(index: int) -> None:
                barrier.wait()
                try:
                    ledgers[index].record_submitting(
                        order(
                            f"aq-{index}",
                            symbol=("AAPL", "NVDA")[index],
                            buy_notional="100",
                        ),
                        123,
                        paper=False,
                        max_daily_buy_notional=Decimal("100"),
                    )
                    outcome = "accepted"
                except RiskLimitError:
                    outcome = "limited"
                with outcomes_lock:
                    outcomes.append(outcome)

            threads = [
                threading.Thread(target=reserve, args=(index,))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertEqual(["accepted", "limited"], sorted(outcomes))
            self.assertEqual(
                Decimal("100"),
                ledgers[0].daily_buy_notional(123, paper=False),
            )

    def test_pending_live_order_blocks_duplicate_from_another_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite3"
            first = OrderLedger(path)
            second = OrderLedger(path)
            first.record_submitting(order("aq-first"), 123, paper=False)

            with self.assertRaisesRegex(RiskLimitError, "禁止重复下单"):
                second.record_submitting(
                    order("aq-second"),
                    123,
                    paper=False,
                )

    def test_live_reservation_rechecks_position_inside_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(order("aq-buy"), 123, paper=False)
            ledger.mark_lifecycle(
                "aq-buy",
                "FILLED",
                filled_quantity=Decimal("1"),
                average_price=Decimal("180"),
            )

            with self.assertRaisesRegex(RiskLimitError, "禁止双向或重复开仓"):
                ledger.record_submitting(
                    order("aq-buy-again"),
                    123,
                    paper=False,
                )
            with self.assertRaisesRegex(RiskLimitError, "超过程序持仓"):
                ledger.record_submitting(
                    order(
                        "aq-oversell",
                        side=Side.SELL,
                        sell_quantity="1.1",
                    ),
                    123,
                    paper=False,
                )

    def test_position_summary_tracks_filled_buy_and_sell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(order("aq-buy"), 123, paper=False)
            ledger.mark_lifecycle(
                "aq-buy",
                "FILLED",
                filled_quantity=Decimal("1"),
                average_price=Decimal("180"),
            )
            ledger.record_submitting(
                order(
                    "aq-sell",
                    side=Side.SELL,
                    sell_quantity="0.4",
                ),
                123,
                paper=False,
            )
            ledger.mark_lifecycle(
                "aq-sell",
                "FILLED",
                filled_quantity=Decimal("0.4"),
                average_price=Decimal("190"),
            )

            position = ledger.position_summary("AAPL", paper=False)

            self.assertEqual(Decimal("0.6"), position.quantity)
            self.assertEqual(Decimal("180"), position.average_price)

    def test_list_filled_records_filters_paper_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(order("paper-buy"), 123, paper=True)
            ledger.mark_lifecycle(
                "paper-buy",
                "FILLED",
                filled_quantity=Decimal("1"),
                average_price=Decimal("100"),
            )
            ledger.record_submitting(order("live-buy"), 123, paper=False)
            ledger.mark_lifecycle(
                "live-buy",
                "FILLED",
                filled_quantity=Decimal("1"),
                average_price=Decimal("101"),
            )

            self.assertEqual(
                ["paper-buy"],
                [record.client_order_id for record in ledger.list_filled_records(True)],
            )
            self.assertEqual(
                ["live-buy"],
                [record.client_order_id for record in ledger.list_filled_records(False)],
            )
            self.assertEqual(2, len(ledger.list_filled_records()))

    def test_unknown_live_order_requires_manual_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(order(), 123, paper=False)
            ledger.mark_unknown("aq-test", "timeout")

            self.assertEqual(1, ledger.unknown_count("AAPL", paper=False))
            self.assertEqual(1, ledger.resolve_unknown("AAPL", paper=False))
            self.assertEqual(0, ledger.unknown_count("AAPL", paper=False))

    def test_portfolio_performance_includes_fees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(
                order("aq-buy", buy_notional="200"), 123, paper=False
            )
            ledger.mark_lifecycle(
                "aq-buy",
                "FILLED",
                filled_quantity=Decimal("2"),
                average_price=Decimal("100"),
                fee=Decimal("2"),
            )
            ledger.record_submitting(
                order("aq-sell", side=Side.SELL, sell_quantity="1"),
                123,
                paper=False,
            )
            ledger.mark_lifecycle(
                "aq-sell",
                "FILLED",
                filled_quantity=Decimal("1"),
                average_price=Decimal("120"),
                fee=Decimal("1"),
            )

            performance = ledger.portfolio_performance(
                paper=False,
                market_prices={"AAPL": Decimal("110")},
            )

            self.assertEqual(Decimal("18"), performance.realized_pnl)
            self.assertEqual(Decimal("9"), performance.unrealized_pnl)
            self.assertEqual(["AAPL"], ledger.open_position_symbols(paper=False))

    def test_portfolio_performance_can_be_filtered_by_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(order("aapl-buy"), 123, paper=True)
            ledger.mark_lifecycle(
                "aapl-buy", "FILLED", filled_quantity=Decimal("1"),
                average_price=Decimal("100"),
            )
            nvda_order = order("nvda-buy", symbol="NVDA")
            ledger.record_submitting(nvda_order, 123, paper=True)
            ledger.mark_lifecycle(
                "nvda-buy", "FILLED", filled_quantity=Decimal("1"),
                average_price=Decimal("200"),
            )

            performance = ledger.portfolio_performance(
                paper=True,
                market_prices={"AAPL": Decimal("110")},
                symbol="aapl",
            )

            self.assertEqual(Decimal("0"), performance.realized_pnl)
            self.assertEqual(Decimal("10"), performance.unrealized_pnl)
            self.assertEqual((), performance.missing_price_symbols)

    def test_short_position_can_only_be_reduced_with_buy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(
                order(
                    "aq-short",
                    side=Side.SELL,
                    buy_notional="200",
                    sell_quantity="0",
                    reduce_only=False,
                    allow_short=True,
                ),
                123,
                paper=True,
            )
            ledger.mark_lifecycle(
                "aq-short",
                "FILLED",
                filled_quantity=Decimal("2"),
                average_price=Decimal("100"),
            )

            position = ledger.position_summary("AAPL", paper=True)
            self.assertEqual(Decimal("-2"), position.quantity)
            self.assertEqual(Decimal("100"), position.average_price)

            with self.assertRaisesRegex(RiskLimitError, "禁止双向或重复开仓"):
                ledger.record_submitting(
                    order("aq-opposite", side=Side.BUY, reduce_only=False),
                    123,
                    paper=True,
                )
            with self.assertRaisesRegex(RiskLimitError, "减仓方向必须是 BUY"):
                ledger.record_submitting(
                    order(
                        "aq-wrong-close",
                        side=Side.SELL,
                        sell_quantity="1",
                        reduce_only=True,
                    ),
                    123,
                    paper=True,
                )

            ledger.record_submitting(
                order(
                    "aq-cover",
                    side=Side.BUY,
                    buy_notional="0",
                    sell_quantity="2",
                    reduce_only=True,
                ),
                123,
                paper=True,
            )
            ledger.mark_lifecycle(
                "aq-cover",
                "FILLED",
                filled_quantity=Decimal("2"),
                average_price=Decimal("90"),
            )
            self.assertEqual(
                Decimal("0"),
                ledger.position_summary("AAPL", paper=True).quantity,
            )

    def test_short_open_requires_provider_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            with self.assertRaisesRegex(RiskLimitError, "不允许建立空头"):
                ledger.record_submitting(
                    order(
                        "aq-short",
                        side=Side.SELL,
                        reduce_only=False,
                        allow_short=False,
                    ),
                    123,
                    paper=True,
                )

    def test_short_portfolio_performance_includes_fees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(
                order(
                    "aq-short",
                    side=Side.SELL,
                    buy_notional="200",
                    reduce_only=False,
                    allow_short=True,
                ),
                123,
                paper=False,
            )
            ledger.mark_lifecycle(
                "aq-short", "FILLED", filled_quantity=Decimal("2"),
                average_price=Decimal("100"), fee=Decimal("2"),
            )
            ledger.record_submitting(
                order(
                    "aq-cover",
                    side=Side.BUY,
                    buy_notional="0",
                    sell_quantity="1",
                    reduce_only=True,
                ),
                123,
                paper=False,
            )
            ledger.mark_lifecycle(
                "aq-cover", "FILLED", filled_quantity=Decimal("1"),
                average_price=Decimal("80"), fee=Decimal("1"),
            )

            performance = ledger.portfolio_performance(
                paper=False,
                market_prices={"AAPL": Decimal("90")},
            )
            self.assertEqual(Decimal("18"), performance.realized_pnl)
            self.assertEqual(Decimal("9"), performance.unrealized_pnl)
            self.assertEqual(
                Decimal("-1"),
                ledger.position_summary("AAPL", paper=False).quantity,
            )

    def test_unrealized_pnl_is_unavailable_when_quote_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(order("aq-buy"), 123, paper=True)
            ledger.mark_lifecycle(
                "aq-buy",
                "FILLED",
                filled_quantity=Decimal("1"),
                average_price=Decimal("100"),
            )

            performance = ledger.portfolio_performance(
                paper=True,
                market_prices={},
            )

            self.assertIsNone(performance.unrealized_pnl)
            self.assertEqual(("AAPL",), performance.missing_price_symbols)


if __name__ == "__main__":
    unittest.main()

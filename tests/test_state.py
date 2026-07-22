from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autoquant.models import OrderRequest, Side
from autoquant.state import OrderLedger


def order(client_order_id: str = "aq-test") -> OrderRequest:
    return OrderRequest(
        symbol="AAPL",
        side=Side.BUY,
        reference_price=Decimal("180"),
        buy_notional=Decimal("100"),
        sell_quantity=Decimal("1"),
        client_order_id=client_order_id,
    )


class OrderLedgerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

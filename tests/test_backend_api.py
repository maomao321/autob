from __future__ import annotations

import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autoquant_backend.runtime import (
    BackendRuntime,
    SECRET_SENTINEL,
    overview_payload,
    snapshot_payload,
)
from autoquant_frontend.client import BackendClient, BackendClientError
from autoquant_shared.config import AppConfig, ConfigStore
from autoquant_backend.server import create_server
from autoquant_backend.state import OrderLedger
from autoquant_shared.models import (
    AccountOverview,
    OrderRequest,
    RuntimeSnapshot,
    Side,
)


class BackendRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = ConfigStore(root / "config.json")
        self.store.save(
            AppConfig(
                symbols=["AAPL"],
                api_key="server-key",
                api_secret="server-secret",
            )
        )
        self.runtime = BackendRuntime(
            config_store=self.store,
            ledger=OrderLedger(root / "orders.sqlite3"),
            desired_state_path=root / "running.json",
        )

    def tearDown(self) -> None:
        self.runtime.shutdown(timeout=0.1)
        self.temporary.cleanup()

    def test_public_config_redacts_credentials(self) -> None:
        payload = self.runtime.public_config()

        self.assertEqual(SECRET_SENTINEL, payload["api_key"])
        self.assertEqual(SECRET_SENTINEL, payload["api_secret"])
        self.assertNotIn("server-key", str(payload))
        self.assertNotIn("server-secret", str(payload))

    def test_saving_sentinel_preserves_server_credentials(self) -> None:
        payload = self.runtime.public_config()
        payload["buy_notional"] = "50.00"

        self.runtime.save_config(payload)

        saved = self.store.load()
        self.assertEqual("server-key", saved.api_key)
        self.assertEqual("server-secret", saved.api_secret)
        self.assertEqual("50.00", saved.buy_notional)

    def test_concurrent_config_updates_are_serialized(self) -> None:
        original_load = self.store.load
        start = threading.Barrier(4)
        state_lock = threading.Lock()
        active_loads = 0
        max_active_loads = 0
        errors: list[Exception] = []

        def tracked_load() -> AppConfig:
            nonlocal active_loads, max_active_loads
            with state_lock:
                active_loads += 1
                max_active_loads = max(max_active_loads, active_loads)
            try:
                time.sleep(0.02)
                return original_load()
            finally:
                with state_lock:
                    active_loads -= 1

        def update(value: int) -> None:
            try:
                start.wait(timeout=1)
                self.runtime.save_config({"ma_period": value})
            except Exception as exc:
                errors.append(exc)

        with patch.object(self.store, "load", side_effect=tracked_load):
            workers = [
                threading.Thread(target=update, args=(value,))
                for value in range(5, 9)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=2)

        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(1, max_active_loads)

    def test_futures_account_overview_uses_provider_quote_asset(self) -> None:
        config = self.store.load()
        config.provider = "binance_futures"
        config.symbols = ["BTCUSDT"]
        self.store.save(config)

        with patch(
            "autoquant_backend.runtime.create_provider"
        ) as create_provider_mock:
            provider = create_provider_mock.return_value
            provider.quote_asset = "USDT"
            provider.get_account_total.return_value = Decimal("123.45")
            payload = self.runtime.account_overview({})

        provider.get_account_total.assert_called_once_with("USDT")
        self.assertEqual("USDT", payload["currency"])
        self.assertEqual("123.45", payload["total_balance"])
        self.assertIn("Binance Futures USDT", payload["message"])

    def test_financial_api_values_have_exactly_two_decimal_places(self) -> None:
        snapshot = snapshot_payload(
            RuntimeSnapshot(
                symbol="AAPL",
                last_price=Decimal("123.456"),
                ma_value=Decimal("120"),
                position_quantity=Decimal("0.123456"),
                average_entry_price=Decimal("119.995"),
                daily_buy_notional=Decimal("100"),
                realized_pnl=Decimal("4.321"),
                unrealized_pnl=Decimal("5.555"),
                profit=Decimal("9.876"),
            )
        )
        overview = overview_payload(
            AccountOverview(
                total_balance=Decimal("1000"),
                realized_pnl=Decimal("1.236"),
                unrealized_pnl=Decimal("-2.5"),
            )
        )

        self.assertEqual("123.46", snapshot["last_price"])
        self.assertEqual("120.00", snapshot["ma_value"])
        self.assertEqual("0.123456", snapshot["position_quantity"])
        self.assertEqual("120.00", snapshot["average_entry_price"])
        self.assertEqual("100.00", snapshot["daily_buy_notional"])
        self.assertEqual("4.32", snapshot["realized_pnl"])
        self.assertEqual("5.56", snapshot["unrealized_pnl"])
        self.assertEqual("9.88", snapshot["profit"])
        self.assertEqual("1000.00", overview["total_balance"])
        self.assertEqual("1.24", overview["realized_pnl"])
        self.assertEqual("-2.50", overview["unrealized_pnl"])

    def test_status_includes_profit_for_stopped_configured_symbol(self) -> None:
        payload = self.runtime.status()

        self.assertEqual(1, len(payload["snapshots"]))
        self.assertEqual("AAPL", payload["snapshots"][0]["symbol"])
        self.assertEqual("STOPPED", payload["snapshots"][0]["state"])
        self.assertEqual("0.00", payload["snapshots"][0]["realized_pnl"])
        self.assertEqual("0.00", payload["snapshots"][0]["unrealized_pnl"])
        self.assertEqual("0.00", payload["snapshots"][0]["profit"])

    def test_status_recomputes_live_symbol_pnl_from_ledger_and_latest_price(self) -> None:
        opening = OrderRequest(
            symbol="AAPL",
            side=Side.BUY,
            reference_price=Decimal("100"),
            buy_notional=Decimal("200"),
            sell_quantity=Decimal("2"),
            client_order_id="pnl-open",
        )
        self.runtime.ledger.record_submitting(opening, 123, paper=True)
        self.runtime.ledger.mark_lifecycle(
            opening.client_order_id,
            "FILLED",
            filled_quantity=Decimal("2"),
            average_price=Decimal("100"),
            fee=Decimal("1"),
        )

        self.runtime._on_snapshot(
            RuntimeSnapshot(
                symbol="AAPL",
                last_price=Decimal("110"),
                position_quantity=Decimal("2"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            )
        )
        first = self.runtime.status()["snapshots"][0]
        self.assertEqual("0.00", first["realized_pnl"])
        self.assertEqual("19.00", first["unrealized_pnl"])

        self.runtime._on_snapshot(
            RuntimeSnapshot(
                symbol="AAPL",
                last_price=Decimal("115"),
                position_quantity=Decimal("2"),
            )
        )
        second = self.runtime.status()["snapshots"][0]
        self.assertEqual("29.00", second["unrealized_pnl"])

        closing = OrderRequest(
            symbol="AAPL",
            side=Side.SELL,
            reference_price=Decimal("120"),
            buy_notional=Decimal("0"),
            sell_quantity=Decimal("2"),
            client_order_id="pnl-close",
            reduce_only=True,
        )
        self.runtime.ledger.record_submitting(closing, 123, paper=True)
        self.runtime.ledger.mark_lifecycle(
            closing.client_order_id,
            "FILLED",
            filled_quantity=Decimal("2"),
            average_price=Decimal("120"),
            fee=Decimal("1"),
        )
        third = self.runtime.status()["snapshots"][0]
        self.assertEqual("38.00", third["realized_pnl"])
        self.assertEqual("0.00", third["unrealized_pnl"])


class BackendHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        store = ConfigStore(root / "config.json")
        store.save(AppConfig(symbols=["AAPL"]))
        self.runtime = BackendRuntime(
            config_store=store,
            ledger=OrderLedger(root / "orders.sqlite3"),
            desired_state_path=root / "running.json",
        )
        self.server = create_server(
            "127.0.0.1", 0, runtime=self.runtime, api_token="test-token"
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.runtime.shutdown(timeout=0.1)
        self.temporary.cleanup()

    def test_health_is_available_without_token(self) -> None:
        client = BackendClient(self.base_url, api_token="")
        self.assertEqual("ok", client.request("GET", "/health")["status"])

    def test_api_rejects_wrong_token(self) -> None:
        client = BackendClient(self.base_url, api_token="wrong")
        with self.assertRaises(BackendClientError):
            client.load_config()

    def test_authenticated_config_round_trip(self) -> None:
        client = BackendClient(self.base_url, api_token="test-token")
        config = client.load_config()
        config.buy_notional = "75.00"
        config.max_order_notional = "75.00"

        saved = client.save_config(config)

        self.assertEqual("75.00", saved.buy_notional)
        self.assertEqual("75.00", self.runtime.config_store.load().buy_notional)

    def test_trade_history_api_returns_financial_fields_without_order_ids(self) -> None:
        from autoquant_shared.models import OrderRequest, Side

        request = OrderRequest(
            symbol="AAPL",
            side=Side.BUY,
            reference_price=Decimal("123.456"),
            buy_notional=Decimal("100"),
            sell_quantity=Decimal("0"),
            client_order_id="aq-private-id",
        )
        self.runtime.ledger.record_submitting(request, 123, paper=True)
        self.runtime.ledger.mark_lifecycle(
            request.client_order_id,
            "FILLED",
            filled_quantity=Decimal("0.5"),
            average_price=Decimal("123.456"),
            fee=Decimal("0.125"),
        )
        client = BackendClient(self.base_url, api_token="test-token")

        payload = client.request(
            "GET",
            "/api/v1/trades?symbol=AAPL&action=OPEN&mode=PAPER&limit=10",
        )

        self.assertEqual(1, payload["count"])
        item = payload["items"][0]
        self.assertEqual("AAPL", item["symbol"])
        self.assertEqual("OPEN", item["action"])
        self.assertEqual("LONG", item["opening_direction"])
        self.assertEqual("123.46", item["price"])
        self.assertEqual("61.73", item["amount"])
        self.assertEqual("0.12", item["fee"])
        self.assertNotIn("order_id", item)
        self.assertNotIn("aq-private-id", str(payload))

    def test_non_loopback_bind_requires_token(self) -> None:
        with self.assertRaises(ValueError):
            create_server("0.0.0.0", 0, runtime=self.runtime, api_token="")

    def test_server_applies_timeout_and_rejects_excess_connections(self) -> None:
        import socket
        from unittest.mock import Mock

        limited = create_server(
            "127.0.0.1",
            0,
            runtime=self.runtime,
            api_token="test-token",
            max_connections=1,
            connection_timeout=0.25,
        )
        try:
            client = socket.create_connection(limited.server_address, timeout=1)
            accepted, _address = limited.get_request()
            try:
                self.assertEqual(0.25, accepted.gettimeout())
            finally:
                accepted.close()
                client.close()

            self.assertTrue(limited._connection_slots.acquire(blocking=False))
            excess = Mock()
            limited.process_request(excess, ("127.0.0.1", 1))
            excess.shutdown.assert_called_once()
            excess.close.assert_called_once()
            limited._connection_slots.release()
        finally:
            limited.server_close()

    def test_server_rejects_invalid_connection_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "并发连接"):
            create_server(
                "127.0.0.1",
                0,
                runtime=self.runtime,
                api_token="test-token",
                max_connections=0,
            )
        with self.assertRaisesRegex(ValueError, "连接超时"):
            create_server(
                "127.0.0.1",
                0,
                runtime=self.runtime,
                api_token="test-token",
                connection_timeout=0,
            )

    def test_remote_client_requires_https_by_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            BackendClient("http://192.0.2.1:8765", api_token="test-token")


if __name__ == "__main__":
    unittest.main()

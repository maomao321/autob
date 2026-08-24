from __future__ import annotations

import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autoquant_backend.runtime import BackendRuntime, SECRET_SENTINEL
from autoquant_frontend.client import BackendClient, BackendClientError
from autoquant_shared.config import AppConfig, ConfigStore
from autoquant_backend.server import create_server
from autoquant_backend.state import OrderLedger


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

    def test_non_loopback_bind_requires_token(self) -> None:
        with self.assertRaises(ValueError):
            create_server("0.0.0.0", 0, runtime=self.runtime, api_token="")

    def test_remote_client_requires_https_by_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            BackendClient("http://192.0.2.1:8765", api_token="test-token")


if __name__ == "__main__":
    unittest.main()

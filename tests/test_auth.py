from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from autoquant_backend.auth import AuthService, UserStore
from autoquant_backend.runtime import BackendRuntime
from autoquant_backend.server import create_server
from autoquant_backend.state import OrderLedger
from autoquant_frontend.services.client import BackendClient, BackendClientError
from autoquant_shared.config import AppConfig, ConfigStore


class UserStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.iterations = patch(
            "autoquant_backend.auth.store.PASSWORD_ITERATIONS", 1_000
        )
        self.iterations.start()
        self.addCleanup(self.iterations.stop)
        self.store = UserStore(Path(self.temporary.name) / "users.sqlite3")

    def test_passwords_are_hashed_and_login_is_case_insensitive(self) -> None:
        user = self.store.create(
            "Admin.User", "correct-horse", role="ADMIN", require_empty=True
        )

        self.assertNotIn(
            b"correct-horse", (Path(self.temporary.name) / "users.sqlite3").read_bytes()
        )
        self.assertEqual(user.user_id, self.store.authenticate("admin.user", "correct-horse").user_id)
        self.assertIsNone(self.store.authenticate("Admin.User", "wrong-password"))

    def test_last_active_admin_cannot_be_removed_or_demoted(self) -> None:
        admin = self.store.create("admin", "password-1", role="ADMIN")

        with self.assertRaisesRegex(RuntimeError, "最后一名管理员"):
            self.store.update(admin.user_id, role="OPERATOR")
        with self.assertRaisesRegex(RuntimeError, "最后一名管理员"):
            self.store.update(admin.user_id, active=False)
        with self.assertRaisesRegex(RuntimeError, "最后一名管理员"):
            self.store.delete(admin.user_id)

    def test_disabling_a_user_revokes_the_session(self) -> None:
        user = self.store.create("operator", "password-1")
        auth = AuthService(self.store, session_seconds=300)
        result = auth.login("operator", "password-1")

        self.assertIsNotNone(auth.authenticate_token(result["token"]))
        self.store.update(user.user_id, active=False)
        self.assertIsNone(auth.authenticate_token(result["token"]))


class UserHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.iterations = patch(
            "autoquant_backend.auth.store.PASSWORD_ITERATIONS", 1_000
        )
        self.iterations.start()
        config_store = ConfigStore(root / "config.json")
        config_store.save(AppConfig(symbols=["AAPL"]))
        self.runtime = BackendRuntime(
            config_store=config_store,
            ledger=OrderLedger(root / "orders.sqlite3"),
            desired_state_path=root / "running.json",
        )
        self.server = create_server(
            "127.0.0.1",
            0,
            runtime=self.runtime,
            api_token="",
            user_store=UserStore(root / "users.sqlite3"),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server.shutdown_runtimes(timeout=0.1)
        self.iterations.stop()
        self.temporary.cleanup()

    def test_setup_login_and_admin_user_management(self) -> None:
        admin_client = BackendClient(self.base_url, api_token="")
        self.assertTrue(admin_client.auth_status()["setup_required"])

        setup = admin_client.setup_admin("admin", "password-1", "系统管理员")
        self.assertEqual("ADMIN", setup["user"]["role"])
        self.assertEqual("admin", admin_client.current_user()["username"])
        operator = admin_client.create_user(
            "trader", "password-2", display_name="交易员"
        )
        self.assertEqual("OPERATOR", operator["role"])
        self.assertEqual(2, len(admin_client.users()))

        operator_client = BackendClient(self.base_url, api_token="")
        operator_client.login("TRADER", "password-2")
        self.assertEqual("trader", operator_client.current_user()["username"])
        self.assertEqual("AAPL", operator_client.load_config().symbols[0])
        with self.assertRaisesRegex(BackendClientError, "仅管理员"):
            operator_client.users()

        admin_client.update_user(operator["user_id"], active=False)
        with self.assertRaises(BackendClientError):
            operator_client.current_user()

    def test_setup_only_runs_once_and_last_admin_is_protected(self) -> None:
        client = BackendClient(self.base_url, api_token="")
        setup = client.setup_admin("admin", "password-1")

        with self.assertRaisesRegex(BackendClientError, "已经完成"):
            BackendClient(self.base_url, api_token="").setup_admin(
                "another", "password-2"
            )
        with self.assertRaisesRegex(BackendClientError, "当前登录账号"):
            client.delete_user(setup["user"]["user_id"])

    def test_user_can_change_password_and_all_sessions_are_revoked(self) -> None:
        first = BackendClient(self.base_url, api_token="")
        first.setup_admin("admin", "password-1")
        second = BackendClient(self.base_url, api_token="")
        second.login("admin", "password-1")

        first.change_password("password-1", "new-password-2")

        with self.assertRaises(BackendClientError):
            second.current_user()
        with self.assertRaisesRegex(BackendClientError, "401"):
            BackendClient(self.base_url, api_token="").login(
                "admin", "password-1"
            )
        replacement = BackendClient(self.base_url, api_token="")
        replacement.login("admin", "new-password-2")
        self.assertEqual("admin", replacement.current_user()["username"])

    def test_business_data_is_isolated_by_authenticated_user(self) -> None:
        admin_client = BackendClient(self.base_url, api_token="")
        admin_client.setup_admin("admin", "password-1")
        admin_client.create_user("trader", "password-2")

        admin_config = admin_client.load_config()
        admin_config.symbols = ["MSFT"]
        admin_client.save_config(admin_config)
        self.runtime._on_log("INFO", "MSFT", "admin-only-log")

        trader_client = BackendClient(self.base_url, api_token="")
        trader_client.login("trader", "password-2")
        trader_config = trader_client.load_config()

        self.assertEqual(["AAPL"], trader_config.symbols)
        self.assertNotIn(
            "admin-only-log",
            str(trader_client.request("GET", "/api/v1/status")),
        )
        trader_config.symbols = ["TSLA"]
        trader_client.save_config(trader_config)
        self.assertEqual(["MSFT"], admin_client.load_config().symbols)

        users = self.server.auth.store.list()
        trader = next(user for user in users if user.username == "trader")
        trader_runtime = self.server.runtime_registry.runtime_for(
            trader.user_id, auth_type="session"
        )
        self.assertNotEqual(
            self.runtime.config_store.path, trader_runtime.config_store.path
        )
        self.assertNotEqual(self.runtime.ledger.path, trader_runtime.ledger.path)
        with patch.dict(
            "os.environ", {"BINANCE_API_KEY": "server-environment-key"}
        ):
            self.assertEqual("", trader_runtime._runner_config().api_key)
            self.assertEqual(
                "server-environment-key", self.runtime._runner_config().api_key
            )


if __name__ == "__main__":
    unittest.main()

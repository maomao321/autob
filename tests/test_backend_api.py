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
from autoquant_backend.backtest import BacktestTrade
from autoquant_frontend.client import (
    BacktestStatusListener,
    BackendClient,
    BackendClientError,
)
from autoquant_shared.config import AppConfig, ConfigStore
from autoquant_backend.server import create_server
from autoquant_backend.state import OrderLedger
from autoquant_shared.models import (
    AccountOverview,
    AiDecisionHistoryItem,
    Bar,
    Direction,
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

    def test_backtest_uses_submitted_strategy_configuration_snapshot(self) -> None:
        submitted = {
            "buy_notional": "125",
            "max_order_notional": "200",
            "max_daily_buy_notional": "500",
            "max_additions_per_position": 2,
            "stop_loss_percent": "1.5",
            "take_profit_percent": "5",
            "max_signal_age_seconds": 45,
            "ai_entry_timing_bars": 80,
        }
        with patch.object(
            self.runtime.backtest_service, "start", return_value="run-1"
        ) as start:
            result = self.runtime.start_backtest(
                {
                    "symbol": "AAPL",
                    "strategy": "five_minute_breakout",
                    "strategy_config": submitted,
                    "download_id": "download-1",
                }
            )

        run_config = start.call_args.args[3]
        self.assertEqual({"accepted": True, "run_id": "run-1"}, result)
        self.assertEqual("125", run_config.buy_notional)
        self.assertEqual("200", run_config.max_order_notional)
        self.assertEqual("500", run_config.max_daily_buy_notional)
        self.assertEqual(2, run_config.max_additions_per_position)
        self.assertEqual("1.5", run_config.stop_loss_percent)
        self.assertEqual("5", run_config.take_profit_percent)
        self.assertEqual(45, run_config.max_signal_age_seconds)
        self.assertEqual(80, run_config.ai_entry_timing_bars)
        self.assertEqual("download-1", start.call_args.kwargs["download_id"])
        self.assertEqual("100.00", self.store.load().buy_notional)

    def test_futures_rankings_reuses_server_cache_and_slices_limit(self) -> None:
        market = {
            "crypto_gainers": [
                {"symbol": "BTCUSDT"},
                {"symbol": "ETHUSDT"},
            ],
            "crypto_losers": [
                {"symbol": "SOLUSDT"},
                {"symbol": "XRPUSDT"},
            ],
            "stock_gainers": [{"symbol": "SOXLUSDT"}],
            "stock_losers": [{"symbol": "MSTRUSDT"}],
            "tickers": {
                "BTCUSDT": {"symbol": "BTCUSDT"},
                "ETHUSDT": {"symbol": "ETHUSDT"},
            },
            "updated_at": 1_700_000_000_000,
            "window": "24h",
        }
        with patch(
            "autoquant_backend.runtime.BinanceFuturesProvider"
        ) as provider_class:
            provider_class.return_value.get_24h_rankings.return_value = market

            first = self.runtime.futures_rankings(limit=1)
            second = self.runtime.futures_rankings(limit=20)

        provider_class.assert_called_once_with(include_daily_stream=False)
        provider_class.return_value.get_24h_rankings.assert_called_once_with(100)
        self.assertEqual([{"symbol": "BTCUSDT"}], first["crypto_gainers"])
        self.assertEqual(2, len(second["crypto_gainers"]))
        self.assertEqual([{"symbol": "SOXLUSDT"}], second["stock_gainers"])
        self.assertEqual(market["tickers"], second["tickers"])

    def test_public_config_redacts_persisted_model_credentials(self) -> None:
        config = self.store.load()
        config.openai_api_key = "saved-openai-key"
        config.deepseek_api_key = "saved-deepseek-key"
        config.qwen_api_key = "saved-qwen-key"
        self.store.save(config)

        payload = self.runtime.public_config()

        self.assertEqual(SECRET_SENTINEL, payload["openai_api_key"])
        self.assertEqual(SECRET_SENTINEL, payload["deepseek_api_key"])
        self.assertEqual(SECRET_SENTINEL, payload["qwen_api_key"])
        self.assertNotIn("saved-openai-key", str(payload))
        self.assertNotIn("saved-deepseek-key", str(payload))
        self.assertNotIn("saved-qwen-key", str(payload))

    def test_saving_sentinel_preserves_server_credentials(self) -> None:
        config = self.store.load()
        config.openai_api_key = "saved-openai-key"
        config.deepseek_api_key = "saved-deepseek-key"
        config.qwen_api_key = "saved-qwen-key"
        self.store.save(config)
        payload = self.runtime.public_config()
        payload["buy_notional"] = "50.00"

        self.runtime.save_config(payload)

        saved = self.store.load()
        self.assertEqual("server-key", saved.api_key)
        self.assertEqual("server-secret", saved.api_secret)
        self.assertEqual("saved-openai-key", saved.openai_api_key)
        self.assertEqual("saved-deepseek-key", saved.deepseek_api_key)
        self.assertEqual("saved-qwen-key", saved.qwen_api_key)
        self.assertEqual("50.00", saved.buy_notional)

    def test_ai_mode_uses_unknown_direction_and_ephemeral_key(self) -> None:
        config = self.store.load()
        config.ai_provider = "CHATGPT"
        self.store.save(config)

        with patch.object(self.runtime.controller, "start") as start_mock:
            self.runtime.start(
                "AAPL",
                "LONG",
                openai_api_key="temporary-openai-key",
            )

        runner_config = start_mock.call_args.args[1]
        self.assertEqual(Direction.UNKNOWN, runner_config.manual_direction)
        self.assertEqual(
            "temporary-openai-key", runner_config.openai_api_key
        )
        self.assertNotIn(
            "temporary-openai-key",
            self.runtime.desired_state_path.read_text(encoding="utf-8"),
        )

    def test_ai_mode_requires_the_selected_provider_key(self) -> None:
        config = self.store.load()
        config.ai_provider = "DEEPSEEK"
        self.store.save(config)

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "DeepSeek API Key"):
                self.runtime.start("AAPL", "FLAT")

    def test_ai_mode_uses_persisted_model_key(self) -> None:
        config = self.store.load()
        config.ai_provider = "CHATGPT"
        config.openai_api_key = "persisted-openai-key"
        self.store.save(config)

        with patch.object(self.runtime.controller, "start") as start_mock:
            self.runtime.start("AAPL", "FLAT")

        runner_config = start_mock.call_args.args[1]
        self.assertEqual("persisted-openai-key", runner_config.openai_api_key)

    def test_qwen_mode_uses_dashscope_environment_key(self) -> None:
        config = self.store.load()
        config.ai_provider = "QWEN"
        self.store.save(config)

        with (
            patch.dict(
                "os.environ", {"DASHSCOPE_API_KEY": "environment-qwen-key"}
            ),
            patch.object(self.runtime.controller, "start") as start_mock,
        ):
            self.runtime.start("AAPL", "FLAT")

        runner_config = start_mock.call_args.args[1]
        self.assertEqual("environment-qwen-key", runner_config.qwen_api_key)
        self.assertEqual(Direction.UNKNOWN, runner_config.manual_direction)

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

    def test_account_overview_reuses_provider_instance(self) -> None:
        with patch(
            "autoquant_backend.runtime.create_provider"
        ) as create_provider_mock:
            provider = create_provider_mock.return_value
            provider.quote_asset = "USDC"
            provider.get_account_total.return_value = Decimal("100")

            self.runtime.account_overview({})
            self.runtime.account_overview({})

        create_provider_mock.assert_called_once()
        self.assertEqual(2, provider.get_account_total.call_count)

    def test_account_overview_uses_recent_runner_price_before_rest_quote(self) -> None:
        opening = OrderRequest(
            symbol="AAPL",
            side=Side.BUY,
            reference_price=Decimal("100"),
            buy_notional=Decimal("200"),
            sell_quantity=Decimal("2"),
            client_order_id="overview-open",
        )
        self.runtime.ledger.record_submitting(opening, 123, paper=True)
        self.runtime.ledger.mark_lifecycle(
            opening.client_order_id,
            "FILLED",
            filled_quantity=Decimal("2"),
            average_price=Decimal("100"),
        )
        self.runtime._on_snapshot(
            RuntimeSnapshot(
                symbol="AAPL",
                last_price=Decimal("110"),
                updated_at=int(time.time() * 1000),
            )
        )

        with patch(
            "autoquant_backend.runtime.create_provider"
        ) as create_provider_mock:
            provider = create_provider_mock.return_value
            provider.quote_asset = "USDC"
            provider.get_account_total.return_value = Decimal("1000")
            payload = self.runtime.account_overview({})

        provider.get_latest_price.assert_not_called()
        self.assertEqual("20.00", payload["unrealized_pnl"])

    def test_financial_api_values_have_exactly_two_decimal_places(self) -> None:
        snapshot = snapshot_payload(
            RuntimeSnapshot(
                symbol="AAPL",
                last_price=Decimal("123.456"),
                ma_value=Decimal("120"),
                position_quantity=Decimal("0.123456"),
                average_entry_price=Decimal("119.995"),
                session_open_notional=Decimal("100"),
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
        self.assertEqual("100.00", snapshot["session_open_notional"])
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

    def test_futures_rankings_api_returns_runtime_payload(self) -> None:
        expected = {
            "stock_gainers": [{"symbol": "SOXLUSDT"}],
            "stock_losers": [{"symbol": "MSTRUSDT"}],
            "crypto_gainers": [{"symbol": "BTCUSDT"}],
            "crypto_losers": [{"symbol": "ETHUSDT"}],
            "updated_at": 1_700_000_000_000,
            "window": "24h",
        }
        client = BackendClient(self.base_url, api_token="test-token")

        with patch.object(
            self.runtime, "futures_rankings", return_value=expected
        ) as rankings_mock:
            payload = client.futures_rankings(limit=12)

        rankings_mock.assert_called_once_with(limit=12)
        self.assertEqual(expected, payload)

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

    def test_ai_decision_api_returns_input_output_and_result(self) -> None:
        self.runtime.ledger.record_ai_decision(
            AiDecisionHistoryItem(
                record_id="decision-api-1",
                decided_at=1_700_000_000_000,
                symbol="SOXLUSDT",
                stage="OPENING_DIRECTION",
                provider="DEEPSEEK",
                model="deepseek-v4-pro",
                outcome="FLAT",
                confidence=0.7,
                summary="暂无明确方向",
                factors=("大盘震荡",),
                risks=("波动较大",),
                input_json='{"context":{"symbol":"SOXLUSDT"}}',
                output_json='[{"response":{"direction":"FLAT"}}]',
                fallback=False,
                elapsed_ms=7031,
                response_ms=6123,
            )
        )
        client = BackendClient(self.base_url, api_token="test-token")

        payload = client.request(
            "GET",
            "/api/v1/ai-decisions?symbol=SOXLUSDT&"
            "stage=OPENING_DIRECTION&limit=10",
        )

        self.assertEqual(1, payload["count"])
        item = payload["items"][0]
        self.assertEqual("deepseek-v4-pro", item["model"])
        self.assertEqual("FLAT", item["outcome"])
        self.assertIn('"symbol":"SOXLUSDT"', item["input_json"])
        self.assertIn('"direction":"FLAT"', item["output_json"])
        self.assertEqual(7031, item["elapsed_ms"])
        self.assertEqual(6123, item["response_ms"])

    def test_backtest_api_lists_persisted_jobs_and_validates_provider(self) -> None:
        client = BackendClient(self.base_url, api_token="test-token")

        self.assertEqual([], client.historical_downloads())
        self.assertEqual([], client.backtest_runs())
        with self.assertRaisesRegex(
            BackendClientError, "仅支持 Binance Futures"
        ):
            client.start_historical_download("AAPL")
        with self.assertRaisesRegex(BackendClientError, "请先完成"):
            client.start_backtest("AAPL", "five_minute_breakout")

    def test_backtest_status_wait_returns_only_after_state_changes(self) -> None:
        client = BackendClient(self.base_url, api_token="test-token")
        initial = client.wait_backtest_status(-1, timeout=0)

        unchanged = client.wait_backtest_status(
            initial["revision"], timeout=0
        )
        self.runtime.backtest_store.create_download(
            "binance_futures", "BTCUSDT", 0, 86_400_000
        )
        changed = client.wait_backtest_status(
            initial["revision"], timeout=0
        )

        self.assertTrue(initial["changed"])
        self.assertFalse(unchanged["changed"])
        self.assertTrue(changed["changed"])
        self.assertEqual("BTCUSDT", changed["downloads"][0]["symbol"])
        self.assertEqual([], changed["runs"])

    def test_backtest_status_listener_receives_backend_notifications(self) -> None:
        client = BackendClient(self.base_url, api_token="test-token")
        received: list[tuple[list[dict], list[dict]]] = []
        errors: list[str] = []
        notified = threading.Event()

        def receive(downloads: list[dict], runs: list[dict]) -> None:
            received.append((downloads, runs))
            notified.set()

        listener = BacktestStatusListener(
            client,
            status_callback=receive,
            error_callback=errors.append,
            wait_timeout=1,
        )
        self.addCleanup(listener.close)
        listener.start()
        self.assertTrue(notified.wait(2))
        notified.clear()

        self.runtime.backtest_store.create_download(
            "binance_futures", "BTCUSDT", 0, 86_400_000
        )

        self.assertTrue(notified.wait(2))
        listener.close()
        self.assertEqual([], errors)
        self.assertEqual("BTCUSDT", received[-1][0][0]["symbol"])
        self.assertEqual([], received[-1][1])

    def test_backtest_download_can_update_to_latest_and_stop_run(self) -> None:
        download_id = self.runtime.backtest_store.create_download(
            "binance_futures", "BTCUSDT", 0, 86_400_000
        )
        self.runtime.backtest_store.update_download(
            download_id,
            status="COMPLETED",
            progress=100,
            completed_at=86_400_000,
        )
        client = BackendClient(self.base_url, api_token="test-token")

        with patch(
            "autoquant_backend.runtime.HistoricalDownloader.start",
            return_value="updated-download",
        ):
            updated_download_id = client.update_historical_download(
                download_id
            )
        with patch.object(
            self.runtime.backtest_service, "cancel"
        ) as cancel:
            client.stop_backtest("run-1")

        self.assertEqual("updated-download", updated_download_id)
        cancel.assert_called_once_with("run-1")

    def test_historical_bars_export_and_import_api_round_trip(self) -> None:
        self.runtime.backtest_store.upsert_bars(
            "binance_stocks",
            [
                Bar(
                    symbol="AAPL",
                    interval="1d",
                    open_time=0,
                    close_time=86_399_999,
                    open=Decimal("100"),
                    high=Decimal("102"),
                    low=Decimal("99"),
                    close=Decimal("101"),
                    volume=Decimal("10"),
                    closed=True,
                ),
                Bar(
                    symbol="AAPL",
                    interval="1d",
                    open_time=86_400_000,
                    close_time=172_799_999,
                    open=Decimal("101"),
                    high=Decimal("103"),
                    low=Decimal("100"),
                    close=Decimal("102"),
                    volume=Decimal("12"),
                    closed=True,
                ),
            ],
        )
        client = BackendClient(self.base_url, api_token="test-token")

        archive = client.export_historical_bars("AAPL")
        result = client.import_historical_bars(
            archive, expected_symbol="AAPL"
        )
        deleted = client.delete_historical_bars(
            "AAPL", "binance_stocks"
        )

        self.assertTrue(archive.startswith(b"PK"))
        self.assertEqual("AAPL", result["symbol"])
        self.assertEqual(2, result["counts"]["1d"])
        self.assertEqual(2, deleted["deleted_bars"])
        self.assertEqual(1, deleted["deleted_downloads"])

    def test_backtest_api_formats_result_metrics_to_two_places(self) -> None:
        config = self.runtime.config_store.load()
        run_id = self.runtime.backtest_store.create_run(
            config.provider,
            "AAPL",
            "five_minute_breakout",
            0,
            86_400_000,
            config,
        )
        self.runtime.backtest_store.complete_run(
            run_id,
            [
                BacktestTrade(
                    side="LONG",
                    entry_time=300_000,
                    exit_time=600_000,
                    entry_price=Decimal("12.345"),
                    exit_price=Decimal("12.523"),
                    quantity=Decimal("8.123"),
                    pnl=Decimal("1.4870887"),
                    exit_reason="TAKE_PROFIT",
                    signal_reason="五分钟突破",
                )
            ],
            total_pnl=Decimal("1.4870887"),
            return_percent=Decimal("1.4870887"),
            max_drawdown_percent=Decimal("34.944173"),
        )
        client = BackendClient(self.base_url, api_token="test-token")

        result = client.backtest_runs()[0]
        details = client.backtest_trade_details(run_id)

        self.assertEqual("1.49", result["total_pnl"])
        self.assertEqual("1.49", result["return_percent"])
        self.assertEqual("34.94", result["max_drawdown_percent"])
        self.assertEqual(1, len(details))
        self.assertEqual("12.34", details[0]["entry_price"])
        self.assertEqual("12.52", details[0]["exit_price"])
        self.assertEqual("8.12", details[0]["quantity"])
        self.assertEqual("1.49", details[0]["pnl"])

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

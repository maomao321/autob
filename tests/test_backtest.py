from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autoquant_backend.backtest import (
    BacktestService,
    BacktestStore,
    BacktestTrade,
    HistoricalArchiveService,
    HistoricalDownloader,
)
from autoquant_shared.config import AppConfig
from autoquant_shared.models import Bar


DAY_MS = 86_400_000
FIVE_MS = 300_000
MINUTE_MS = 60_000


def make_bar(
    interval: str,
    open_time: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
) -> Bar:
    step = {"1d": DAY_MS, "5m": FIVE_MS, "1m": MINUTE_MS}[interval]
    price = Decimal(close)
    return Bar(
        symbol="BTCUSDT",
        interval=interval,
        open_time=open_time,
        close_time=open_time + step - 1,
        open=price,
        high=Decimal(high) if high else price,
        low=Decimal(low) if low else price,
        close=price,
        volume=Decimal("1"),
        closed=True,
    )


class BacktestTests(unittest.TestCase):
    def test_store_status_waiter_is_notified_when_download_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            revision = store.wait_for_status_change(-1, 0)
            download_id = store.create_download(
                "binance_futures", "BTCUSDT", 0, DAY_MS
            )
            revision = store.wait_for_status_change(revision, 0)
            observed: list[int] = []
            waiter = threading.Thread(
                target=lambda: observed.append(
                    store.wait_for_status_change(revision, 1)
                )
            )
            waiter.start()

            store.update_download(
                download_id, status="RUNNING", message="正在下载"
            )
            waiter.join(timeout=1)

            self.assertFalse(waiter.is_alive())
            self.assertEqual([revision + 1], observed)

    def test_download_records_track_updated_at(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            download_id = store.create_download(
                "binance_futures", "BTCUSDT", 0, DAY_MS
            )
            created = store.get_download(download_id)

            store.update_download(
                download_id, status="RUNNING", progress=25
            )
            updated = store.get_download(download_id)

            self.assertIsNotNone(created)
            self.assertIsNotNone(updated)
            self.assertGreater(int(created["updated_at"]), 0)
            self.assertGreaterEqual(
                int(updated["updated_at"]), int(created["updated_at"])
            )

    def test_backtest_can_be_started_and_stopped_for_download_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            download_id = store.create_download(
                "binance_futures", "BTCUSDT", 0, DAY_MS
            )
            store.update_download(
                download_id,
                status="COMPLETED",
                progress=100,
                completed_at=DAY_MS,
            )
            service = BacktestService(store)
            config = AppConfig(
                symbols=["BTCUSDT"], provider="binance_futures"
            )
            simulation_started = threading.Event()

            def simulate(*_args, **kwargs):
                simulation_started.set()
                cancel_event = kwargs["cancel_event"]
                cancel_event.wait(2)
                return []

            with patch.object(
                BacktestService, "_simulate", side_effect=simulate
            ):
                run_id = service.start(
                    "binance_futures",
                    "BTCUSDT",
                    "five_minute_breakout",
                    config,
                    download_id=download_id,
                )
                self.assertTrue(simulation_started.wait(1))
                service.cancel(run_id)
                for _ in range(100):
                    if store.get_run(run_id)["status"] == "CANCELLED":
                        break
                    threading.Event().wait(0.01)

            run = store.get_run(run_id)
            self.assertEqual(download_id, run["download_id"])
            self.assertEqual("CANCELLED", run["status"])

    def test_existing_backtest_database_adds_strategy_snapshot_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE market_downloads (
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
                    CREATE TABLE backtest_runs (
                        run_id TEXT PRIMARY KEY,
                        provider TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        start_time INTEGER NOT NULL,
                        end_time INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        config_json TEXT NOT NULL,
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

            store = BacktestStore(path)

            with closing(store._connect()) as connection:
                run_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(backtest_runs)"
                    ).fetchall()
                }
                download_columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(market_downloads)"
                    ).fetchall()
                }
            self.assertIn("strategy_config_json", run_columns)
            self.assertIn("download_id", run_columns)
            self.assertIn("updated_at", download_columns)

    def test_store_upserts_candles_and_persists_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            first = make_bar("1m", 0, "10")
            changed = make_bar("1m", 0, "11")

            store.upsert_bars("binance_futures", [first, changed])

            bars = store.load_bars(
                "binance_futures", "BTCUSDT", "1m", 0, MINUTE_MS
            )
            self.assertEqual(1, len(bars))
            self.assertEqual(Decimal("11"), bars[0].close)

    def test_backtest_uses_daily_direction_and_one_minute_risk_exit(self) -> None:
        config = AppConfig(
            symbols=["BTCUSDT"],
            provider="binance_futures",
            ma_period=3,
            buy_notional="100",
            stop_loss_percent="2",
            take_profit_percent="4",
            max_additions_per_position=1,
        )
        config.validate()
        daily = [
            make_bar("1d", 0, "99"),
            make_bar("1d", DAY_MS, "100"),
            make_bar("1d", 2 * DAY_MS, "101"),
        ]
        setup_closes = ["10"] * 18 + ["11"] * 6 + ["12", "12", "12"]
        five = [
            make_bar("5m", 2 * DAY_MS + index * FIVE_MS, close)
            for index, close in enumerate(setup_closes)
        ]
        minute = [
            make_bar(
                "1m",
                2 * DAY_MS + 131 * MINUTE_MS,
                "12.6",
                high="12.6",
                low="12",
            )
        ]

        trades = BacktestService._simulate(
            "BTCUSDT", daily, five, minute, config
        )

        self.assertEqual(1, len(trades))
        self.assertEqual("LONG", trades[0].side)
        self.assertEqual("TAKE_PROFIT", trades[0].exit_reason)
        self.assertEqual(Decimal("4"), trades[0].pnl)

    def test_backtest_adds_once_then_reopens_after_full_exit(self) -> None:
        config = AppConfig(
            symbols=["BTCUSDT"],
            provider="binance_futures",
            buy_notional="100",
            max_additions_per_position=1,
            stop_loss_percent="2",
            take_profit_percent="4",
        )
        config.validate()
        daily = [
            make_bar("1d", 0, "99"),
            make_bar("1d", DAY_MS, "100"),
            make_bar("1d", 2 * DAY_MS, "101"),
        ]
        closes = ["10"] * 18 + ["11"] * 6 + [
            "12", "13", "14", "15", "16"
        ]
        five = [
            make_bar("5m", 2 * DAY_MS + index * FIVE_MS, close)
            for index, close in enumerate(closes)
        ]
        minute = [
            make_bar(
                "1m",
                2 * DAY_MS + 141 * MINUTE_MS,
                "15",
                high="15",
                low="13.5",
            ),
            make_bar(
                "1m",
                2 * DAY_MS + 146 * MINUTE_MS,
                "16",
            ),
        ]

        trades = BacktestService._simulate(
            "BTCUSDT", daily, five, minute, config
        )

        self.assertEqual(2, len(trades))
        self.assertEqual("TAKE_PROFIT", trades[0].exit_reason)
        self.assertGreater(trades[0].quantity, Decimal("100") / Decimal("12"))
        self.assertEqual("END_OF_DATA", trades[1].exit_reason)
        self.assertGreater(trades[1].entry_time, trades[0].exit_time)

    def test_run_configuration_does_not_persist_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite3"
            store = BacktestStore(path)
            config = AppConfig(
                symbols=["BTCUSDT"],
                provider="binance_futures",
                api_key="binance-key",
                api_secret="binance-secret",
                openai_api_key="openai-key",
                deepseek_api_key="deepseek-key",
                qwen_api_key="qwen-key",
            )
            run_id = store.create_run(
                "binance_futures",
                "BTCUSDT",
                "five_minute_breakout",
                0,
                DAY_MS,
                config,
            )

            with closing(store._connect()) as connection:
                row = connection.execute(
                    "SELECT config_json, strategy_config_json "
                    "FROM backtest_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            payload = json.loads(row["config_json"])
            strategy_payload = json.loads(row["strategy_config_json"])
            self.assertEqual("", payload["api_key"])
            self.assertEqual("", payload["api_secret"])
            self.assertEqual("", payload["openai_api_key"])
            self.assertEqual("", payload["deepseek_api_key"])
            self.assertEqual("", payload["qwen_api_key"])
            self.assertNotIn("api_key", strategy_payload)
            self.assertNotIn("api_secret", strategy_payload)
            self.assertEqual("five_minute_breakout", strategy_payload["strategy"])

    def test_run_keeps_an_immutable_strategy_configuration_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            config = AppConfig(
                symbols=["BTCUSDT"],
                provider="binance_futures",
                buy_notional="125",
                max_order_notional="200",
                max_daily_buy_notional="500",
                max_additions_per_position=3,
                stop_loss_percent="1.5",
                take_profit_percent="6",
                max_signal_age_seconds=45,
                ai_entry_timing_bars=80,
            )
            config.validate()
            store.create_run(
                "binance_futures",
                "BTCUSDT",
                "five_minute_breakout",
                0,
                DAY_MS,
                config,
            )

            config.buy_notional = "999"
            snapshot = store.list_runs()[0]["strategy_config"]

            self.assertEqual("五分钟突破", snapshot["strategy_name"])
            self.assertEqual("5m", snapshot["kline_interval"])
            self.assertEqual(7, snapshot["fast_ma_period"])
            self.assertEqual(25, snapshot["slow_ma_period"])
            self.assertEqual("125", snapshot["buy_notional"])
            self.assertEqual("200", snapshot["max_order_notional"])
            self.assertEqual("500", snapshot["max_daily_buy_notional"])
            self.assertEqual(3, snapshot["max_additions_per_position"])
            self.assertEqual("1.5", snapshot["stop_loss_percent"])
            self.assertEqual("6", snapshot["take_profit_percent"])
            self.assertEqual(45, snapshot["max_signal_age_seconds"])
            self.assertEqual(80, snapshot["ai_entry_timing_bars"])

    def test_historical_archive_round_trip_is_scoped_to_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_store = BacktestStore(root / "source.sqlite3")
            for interval, step in (
                ("1d", DAY_MS),
                ("5m", FIVE_MS),
                ("1m", MINUTE_MS),
            ):
                source_store.upsert_bars(
                    "binance_futures",
                    [
                        make_bar(interval, 0, "10"),
                        make_bar(interval, step, "11"),
                    ],
                )
            archive, filename = HistoricalArchiveService(source_store).export(
                "binance_futures", "BTCUSDT"
            )
            target_store = BacktestStore(root / "target.sqlite3")

            result = HistoricalArchiveService(target_store).import_archive(
                archive, expected_symbol="BTCUSDT"
            )

            self.assertEqual("BTCUSDT", result["symbol"])
            self.assertEqual("binance_futures", result["provider"])
            self.assertEqual({"1d": 2, "5m": 2, "1m": 2}, result["counts"])
            self.assertIn("BTCUSDT", filename)
            self.assertEqual(
                2,
                target_store.count_bars(
                    "binance_futures", "BTCUSDT", "1m", 0, 2 * MINUTE_MS
                ),
            )
            self.assertEqual(
                "COMPLETED", target_store.list_downloads()[0]["status"]
            )
            target_store.create_run(
                "binance_futures",
                "BTCUSDT",
                "five_minute_breakout",
                0,
                DAY_MS,
                AppConfig(
                    symbols=["BTCUSDT"], provider="binance_futures"
                ),
            )

            deleted = target_store.delete_historical_bars(
                "binance_futures", "BTCUSDT"
            )

            self.assertEqual(6, deleted["deleted_bars"])
            self.assertEqual(1, deleted["deleted_downloads"])
            self.assertEqual(1, len(target_store.list_runs()))

    def test_historical_archive_rejects_selected_symbol_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            store.upsert_bars(
                "binance_futures",
                [make_bar("1m", 0, "10"), make_bar("1m", MINUTE_MS, "11")],
            )
            service = HistoricalArchiveService(store)
            archive, _filename = service.export("binance_futures", "BTCUSDT")

            with self.assertRaisesRegex(ValueError, "与页面指定标的"):
                service.import_archive(archive, expected_symbol="ETHUSDT")

    def test_short_listing_history_download_is_completed_with_actual_counts(self) -> None:
        class ShortHistoryProvider:
            def get_historical_bars(
                self,
                symbol: str,
                interval: str,
                start_time: int,
                end_time: int,
                limit: int,
            ) -> list[Bar]:
                return [make_bar(interval, start_time, "10")]

        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            download_id = store.create_download(
                "binance_futures", "BTCUSDT", 0, DAY_MS - 1
            )
            downloader = HistoricalDownloader(
                store, ShortHistoryProvider, "binance_futures"
            )

            downloader._run(download_id, "BTCUSDT", 0, DAY_MS - 1)

            result = store.list_downloads()[0]
            self.assertEqual("COMPLETED", result["status"])
            self.assertEqual(1, result["daily_count"])
            self.assertEqual(1, result["five_minute_count"])
            self.assertEqual(1, result["one_minute_count"])

    def test_historical_download_appends_usdt_to_short_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            downloader = HistoricalDownloader(
                store, lambda: object(), "binance_futures"
            )

            with patch(
                "autoquant_backend.backtest.downloader.threading.Thread"
            ):
                download_id = downloader.start("dram")

            download = store.get_download(download_id)
            self.assertEqual("binance_futures", download["provider"])
            self.assertEqual("DRAMUSDT", download["symbol"])

    def test_historical_download_resumes_after_last_persisted_bar(self) -> None:
        class CapturingProvider:
            def __init__(self) -> None:
                self.start_times: list[int] = []

            def get_historical_bars(
                self,
                symbol: str,
                interval: str,
                start_time: int,
                end_time: int,
                limit: int,
            ) -> list[Bar]:
                self.start_times.append(start_time)
                return []

        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            store.upsert_bars(
                "binance_futures", [make_bar("1m", 0, "10")]
            )
            download_id = store.create_download(
                "binance_futures", "BTCUSDT", 0, MINUTE_MS * 2 - 1
            )
            provider = CapturingProvider()
            downloader = HistoricalDownloader(
                store, lambda: provider, "binance_futures"
            )

            count = downloader._download_interval(
                download_id,
                provider,
                "BTCUSDT",
                "1m",
                0,
                MINUTE_MS * 2 - 1,
                2,
            )

            self.assertEqual([MINUTE_MS], provider.start_times)
            self.assertEqual(1, count)

    def test_backtest_with_too_few_samples_completes_with_zero_trades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            for interval in ("1d", "5m", "1m"):
                store.upsert_bars(
                    "binance_futures", [make_bar(interval, 0, "10")]
                )
            config = AppConfig(
                symbols=["BTCUSDT"], provider="binance_futures"
            )
            run_id = store.create_run(
                "binance_futures",
                "BTCUSDT",
                "five_minute_breakout",
                0,
                DAY_MS,
                config,
            )

            BacktestService(store)._run(
                run_id,
                "binance_futures",
                "BTCUSDT",
                {"start_time": 0, "end_time": DAY_MS},
                config,
            )

            result = store.list_runs()[0]
            self.assertEqual("COMPLETED", result["status"])
            self.assertEqual(0, result["trade_count"])

    def test_historical_delete_rejects_active_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            store.upsert_bars(
                "binance_futures", [make_bar("1m", 0, "10")]
            )
            store.create_download(
                "binance_futures", "BTCUSDT", 0, DAY_MS
            )

            with self.assertRaisesRegex(RuntimeError, "仍在下载"):
                store.delete_historical_bars(
                    "binance_futures", "BTCUSDT"
                )

            self.assertEqual(
                1,
                store.count_bars(
                    "binance_futures", "BTCUSDT", "1m", 0, MINUTE_MS
                ),
            )

    def test_backtest_trade_details_are_persisted_by_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BacktestStore(Path(directory) / "orders.sqlite3")
            config = AppConfig(
                symbols=["BTCUSDT"], provider="binance_futures"
            )
            run_id = store.create_run(
                "binance_futures",
                "BTCUSDT",
                "five_minute_breakout",
                0,
                DAY_MS,
                config,
            )
            trade = BacktestTrade(
                side="LONG",
                entry_time=FIVE_MS,
                exit_time=2 * FIVE_MS,
                entry_price=Decimal("12.345"),
                exit_price=Decimal("12.523"),
                quantity=Decimal("8.1"),
                pnl=Decimal("1.4418"),
                exit_reason="TAKE_PROFIT",
                signal_reason="五分钟突破",
            )
            store.complete_run(
                run_id,
                [trade],
                total_pnl=trade.pnl,
                return_percent=Decimal("1.4418"),
                max_drawdown_percent=Decimal("0"),
            )

            details = store.backtest_trades(run_id)

            self.assertEqual(1, len(details))
            self.assertEqual("LONG", details[0]["side"])
            self.assertEqual("12.345", details[0]["entry_price"])
            self.assertEqual("TAKE_PROFIT", details[0]["exit_reason"])
            self.assertEqual("五分钟突破", details[0]["signal_reason"])


if __name__ == "__main__":
    unittest.main()

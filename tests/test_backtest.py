from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

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
            max_trades_per_day=1,
        )
        config.validate()
        daily = [
            make_bar("1d", 0, "99"),
            make_bar("1d", DAY_MS, "100"),
            make_bar("1d", 2 * DAY_MS, "101"),
        ]
        five = [
            make_bar("5m", 2 * DAY_MS + index * FIVE_MS, close)
            for index, close in enumerate(("10", "10", "10", "12", "12"))
        ]
        minute = [
            make_bar(
                "1m",
                2 * DAY_MS + 20 * MINUTE_MS,
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
                    "SELECT config_json FROM backtest_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            payload = json.loads(row["config_json"])
            self.assertEqual("", payload["api_key"])
            self.assertEqual("", payload["api_secret"])
            self.assertEqual("", payload["openai_api_key"])
            self.assertEqual("", payload["deepseek_api_key"])

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

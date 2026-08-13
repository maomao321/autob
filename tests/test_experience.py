from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autoquant.experience import (
    MarketBar,
    OpenAIVectorStoreUploader,
    extract_trade_experiences,
    load_ohlcv_csv,
    merge_experience_document,
)
from autoquant.state import OrderRecord


def filled_order(
    client_order_id: str,
    side: str,
    *,
    created_at: int,
    quantity: str,
    price: str,
    fee: str = "0",
    paper: bool = True,
) -> OrderRecord:
    return OrderRecord(
        client_order_id=client_order_id,
        symbol="AAPL",
        side=side,
        trading_day=1,
        status="FILLED",
        order_id=client_order_id,
        paper=paper,
        created_at=created_at,
        updated_at=created_at,
        message="",
        requested_notional=Decimal("0"),
        requested_quantity=Decimal("0"),
        filled_quantity=Decimal(quantity),
        average_price=Decimal(price),
        fee=Decimal(fee),
    )


class ExperienceTests(unittest.TestCase):
    def test_extracts_wins_and_losses_with_fifo_fee_allocation(self) -> None:
        records = [
            filled_order(
                "buy", "BUY", created_at=10_000, quantity="2", price="100", fee="2"
            ),
            filled_order(
                "sell-win",
                "SELL",
                created_at=20_000,
                quantity="1",
                price="120",
                fee="1",
            ),
            filled_order(
                "sell-loss",
                "SELL",
                created_at=30_000,
                quantity="1",
                price="90",
                fee="1",
            ),
        ]

        experiences = extract_trade_experiences(records)

        self.assertEqual(["WIN", "LOSS"], [item.outcome for item in experiences])
        self.assertEqual("18", experiences[0].net_pnl)
        self.assertEqual("-12", experiences[1].net_pnl)
        self.assertEqual("1", experiences[0].entry_fee)
        self.assertEqual("1", experiences[1].entry_fee)

    def test_kline_pattern_never_uses_post_entry_bars(self) -> None:
        records = [
            filled_order(
                "buy", "BUY", created_at=30_000, quantity="1", price="100"
            ),
            filled_order(
                "sell", "SELL", created_at=50_000, quantity="1", price="110"
            ),
        ]
        bars = [
            MarketBar(
                symbol="AAPL",
                timestamp_ms=timestamp,
                open=Decimal(str(price)),
                high=Decimal(str(price + 1)),
                low=Decimal(str(price - 1)),
                close=Decimal(str(price)),
                volume=Decimal("10"),
                interval="1m",
            )
            for timestamp, price in ((10_000, 100), (20_000, 102), (40_000, 999))
        ]

        experience = extract_trade_experiences(
            records, bars_by_symbol={"AAPL": bars}, pattern_bars=5
        )[0]
        pattern = experience.pre_entry_pattern

        self.assertTrue(pattern["available"])
        self.assertEqual(2, pattern["bar_count"])
        self.assertEqual(20_000, pattern["end_time_ms"])
        self.assertNotIn("999", json.dumps(pattern))

    def test_loads_standard_ohlcv_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            path.write_text(
                "symbol,timestamp,open,high,low,close,volume,interval\n"
                "AAPL,2026-08-13T09:30:00Z,100,102,99,101,1200,1m\n",
                encoding="utf-8",
            )

            bars = load_ohlcv_csv(path)

            self.assertEqual(1, len(bars["AAPL"]))
            self.assertEqual(Decimal("101"), bars["AAPL"][0].close)
            self.assertEqual("1m", bars["AAPL"][0].interval)

    def test_open_time_is_shifted_to_bar_close_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            path.write_text(
                "symbol,open_time,open,high,low,close,interval\n"
                "AAPL,10000,100,102,99,101,1m\n",
                encoding="utf-8",
            )

            bar = load_ohlcv_csv(path)["AAPL"][0]

            self.assertEqual(10_060_000, bar.timestamp_ms)

    def test_local_library_merge_is_idempotent(self) -> None:
        records = [
            filled_order(
                "buy", "BUY", created_at=10_000, quantity="1", price="100"
            ),
            filled_order(
                "sell", "SELL", created_at=20_000, quantity="1", price="110"
            ),
        ]
        experiences = extract_trade_experiences(records)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiences.json"

            _path, first_added, first_total = merge_experience_document(
                path, experiences
            )
            _path, second_added, second_total = merge_experience_document(
                path, experiences
            )

            self.assertEqual((1, 1), (first_added, first_total))
            self.assertEqual((0, 1), (second_added, second_total))
            self.assertEqual(1, json.loads(path.read_text(encoding="utf-8"))["count"])

    def test_uploader_creates_store_uploads_file_and_attaches_it(self) -> None:
        calls: list[tuple[str, dict]] = []

        def request_json(url, payload, _api_key, _timeout):
            calls.append((url, payload))
            if url.endswith("/vector_stores"):
                return {"id": "vs_test"}
            return {"id": "attach_test", "status": "in_progress"}

        def upload_file(path, _api_key, _timeout):
            self.assertEqual("experiences.json", path.name)
            return {"id": "file-test"}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiences.json"
            path.write_text("{}", encoding="utf-8")
            result = OpenAIVectorStoreUploader(
                request_json=request_json, upload_file=upload_file
            ).upload(path, api_key="secret")

        self.assertEqual("vs_test", result.vector_store_id)
        self.assertEqual("file-test", result.file_id)
        self.assertTrue(calls[1][0].endswith("/vs_test/files"))
        self.assertEqual({"file_id": "file-test"}, calls[1][1])


if __name__ == "__main__":
    unittest.main()

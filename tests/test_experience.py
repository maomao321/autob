from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook

from autoquant.experience import (
    ExperienceError,
    OpenAIVectorStoreUploader,
    import_external_experiences,
    load_external_klines,
    merge_experience_document,
)


class ExperienceTests(unittest.TestCase):
    def test_imports_long_and_short_trade_records_from_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trades.csv"
            path.write_text(
                "trade_id,symbol,side,entry_time,exit_time,entry_price,"
                "exit_price,quantity,fee,notes\n"
                "T001,AAPL,LONG,2026-08-13T09:35:00Z,"
                "2026-08-13T09:45:00Z,100,110,2,1,突破后回踩\n"
                "T002,MSFT,SHORT,2026-08-13T10:00:00Z,"
                "2026-08-13T10:05:00Z,100,110,1,0.5,假突破失败\n",
                encoding="utf-8",
            )

            result = import_external_experiences(trade_path=path)

        self.assertEqual(2, result.trade_rows)
        self.assertEqual(0, result.kline_rows)
        self.assertEqual(
            ["WIN", "LOSS"], [item.outcome for item in result.experiences]
        )
        self.assertEqual("19", result.experiences[0].net_pnl)
        self.assertEqual("-10.5", result.experiences[1].net_pnl)
        self.assertTrue(
            all(item.source == "external_trade_file" for item in result.experiences)
        )

    def test_matching_kline_group_uses_only_pre_entry_closed_bars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trade_path = root / "trades.csv"
            trade_path.write_text(
                "trade_id,symbol,entry_time,exit_time,entry_price,exit_price,quantity\n"
                "T001,AAPL,2026-08-13T09:35:00Z,"
                "2026-08-13T09:45:00Z,100,110,1\n",
                encoding="utf-8",
            )
            kline_path = root / "patterns.csv"
            kline_path.write_text(
                "pattern_id,symbol,close_time,open,high,low,close,volume,interval\n"
                "T001,AAPL,2026-08-13T09:33:00Z,100,102,99,101,1000,1m\n"
                "T001,AAPL,2026-08-13T09:34:00Z,101,103,100,102,1200,1m\n"
                "T001,AAPL,2026-08-13T09:36:00Z,999,1001,998,1000,5000,1m\n",
                encoding="utf-8",
            )

            result = import_external_experiences(
                trade_path=trade_path,
                kline_path=kline_path,
                pattern_bars=5,
            )

        self.assertEqual(1, len(result.experiences))
        pattern = result.experiences[0].pre_entry_pattern
        self.assertTrue(pattern["available"])
        self.assertEqual(2, pattern["bar_count"])
        self.assertEqual("2026-08-13T09:34:00+00:00", _iso(pattern["end_time_ms"]))
        self.assertNotIn("999", json.dumps(pattern))

    def test_imports_standalone_kline_pattern_from_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "patterns.csv"
            path.write_text(
                "pattern_id,pattern_name,symbol,close_time,open,high,low,close,volume,interval\n"
                "P001,杯柄突破,AAPL,2026-08-13T09:33:00Z,100,102,99,101,1000,1m\n"
                "P001,杯柄突破,AAPL,2026-08-13T09:34:00Z,101,104,100,103,1500,1m\n",
                encoding="utf-8",
            )

            result = import_external_experiences(kline_path=path)

        self.assertEqual(0, result.trade_rows)
        self.assertEqual(2, result.kline_rows)
        self.assertEqual(1, len(result.experiences))
        experience = result.experiences[0]
        self.assertEqual("KLINE_PATTERN", experience.record_type)
        self.assertEqual("UNLABELED", experience.outcome)
        self.assertEqual("杯柄突破", experience.pre_entry_pattern["pattern_name"])

    def test_imports_chinese_columns_from_xlsx(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "交易记录.xlsx"
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.append(
                [
                    "交易编号",
                    "股票代码",
                    "方向",
                    "开仓时间",
                    "平仓时间",
                    "开仓价",
                    "平仓价",
                    "数量",
                    "手续费",
                ]
            )
            worksheet.append(
                [
                    "CN001",
                    "AAPL",
                    "多",
                    "2026-08-13T09:35:00Z",
                    "2026-08-13T09:45:00Z",
                    100,
                    105,
                    2,
                    1,
                ]
            )
            workbook.save(path)
            workbook.close()

            result = import_external_experiences(trade_path=path)

        self.assertEqual(1, len(result.experiences))
        self.assertEqual("CN001", result.experiences[0].external_id)
        self.assertEqual("9", result.experiences[0].net_pnl)

    def test_open_time_is_shifted_to_bar_close_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            path.write_text(
                "symbol,open_time,open,high,low,close,interval\n"
                "AAPL,10000,100,102,99,101,1m\n",
                encoding="utf-8",
            )

            bar = load_external_klines(path)[0]

        self.assertEqual(10_060_000, bar.timestamp_ms)
        self.assertEqual(Decimal("101"), bar.close)

    def test_local_library_merge_is_idempotent_and_external_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trade_path = root / "trades.csv"
            trade_path.write_text(
                "trade_id,symbol,entry_time,exit_time,entry_price,exit_price,quantity\n"
                "T001,AAPL,10000,20000,100,110,1\n",
                encoding="utf-8",
            )
            experiences = import_external_experiences(
                trade_path=trade_path
            ).experiences
            library_path = root / "experiences.json"

            _path, first_added, first_total = merge_experience_document(
                library_path, experiences
            )
            _path, second_added, second_total = merge_experience_document(
                library_path, experiences
            )
            payload = json.loads(library_path.read_text(encoding="utf-8"))

        self.assertEqual((1, 1), (first_added, first_total))
        self.assertEqual((0, 1), (second_added, second_total))
        self.assertEqual(2, payload["schema_version"])
        self.assertEqual("external_trade_file", payload["experiences"][0]["source"])

    def test_merge_rejects_old_local_ledger_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiences.json"
            path.write_text(
                json.dumps({"schema_version": 1, "experiences": []}),
                encoding="utf-8",
            )

            with self.assertRaises(ExperienceError):
                merge_experience_document(path, [])

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


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autoquant.config import AppConfig, ConfigStore, normalize_symbols


class ConfigTests(unittest.TestCase):
    def test_normalize_symbols_deduplicates_and_uppercases(self) -> None:
        self.assertEqual(["AAPL", "BRK.B"], normalize_symbols([" aapl ", "BRK.B", "AAPL"]))

    def test_config_round_trip_does_not_have_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            config = AppConfig(symbols=["AAPL", "NVDA"], ma_period=8)
            store.save(config)

            loaded = store.load()

            self.assertEqual(["AAPL", "NVDA"], loaded.symbols)
            self.assertEqual(8, loaded.ma_period)
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("api_secret", content.lower())
            self.assertNotIn("api_key", content.lower())

    def test_invalid_live_parameters_are_rejected(self) -> None:
        config = AppConfig(symbols=["AAPL"], trading_mode="REAL", buy_notional="0")
        with self.assertRaisesRegex(ValueError, "买入金额"):
            config.validate()

    def test_non_finite_amount_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "买入金额"):
            AppConfig(symbols=["AAPL"], buy_notional="Infinity").validate()

    def test_unicode_symbol_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "股票代码格式"):
            normalize_symbols(["ＡＡＰＬ"])

    def test_non_binance_api_host_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Binance 官方"):
            AppConfig(
                symbols=["AAPL"], rest_base_url="https://example.com"
            ).validate()

    def test_buy_amount_must_fit_risk_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "单笔金额上限"):
            AppConfig(
                symbols=["AAPL"],
                buy_notional="101",
                max_order_notional="100",
            ).validate()

    def test_symbol_count_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能超过"):
            AppConfig(symbols=[f"A{index}" for index in range(21)]).validate()


if __name__ == "__main__":
    unittest.main()

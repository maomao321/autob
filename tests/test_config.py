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


if __name__ == "__main__":
    unittest.main()


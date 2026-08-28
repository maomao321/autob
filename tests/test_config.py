from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from autoquant_shared.config import (
    AppConfig,
    ConfigStore,
    credential_or_environment,
    normalize_symbols,
)


class ConfigTests(unittest.TestCase):
    def test_empty_symbol_list_can_be_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")

            store.save(AppConfig(symbols=[]))

            self.assertEqual([], store.load().symbols)

    def test_normalize_symbols_deduplicates_and_uppercases(self) -> None:
        self.assertEqual(["AAPL", "BRK.B"], normalize_symbols([" aapl ", "BRK.B", "AAPL"]))

    def test_config_round_trip_includes_binance_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path)
            config = AppConfig(
                symbols=["AAPL", "NVDA"],
                manual_directions={"aapl": "long", "NVDA": "AUTO"},
                api_key="binance-key",
                api_secret="binance-secret",
                ma_period=8,
                max_order_notional="250",
                ai_provider="dual",
                ai_min_confidence="0.75",
                openai_api_key="openai-secret",
                deepseek_api_key="deepseek-secret",
                qwen_api_key="qwen-secret",
                qwen_model="qwen-plus",
                openai_reasoning_enabled=True,
                openai_reasoning_effort="high",
                qwen_thinking_enabled=True,
                qwen_reasoning_effort="medium",
                deepseek_thinking_enabled=True,
                deepseek_reasoning_effort="max",
                ai_entry_timing_bars=90,
            )
            store.save(config)

            loaded = store.load()

            self.assertEqual(["AAPL", "NVDA"], loaded.symbols)
            self.assertEqual(
                {"AAPL": "LONG", "NVDA": "FLAT"},
                loaded.manual_directions,
            )
            self.assertEqual(8, loaded.ma_period)
            self.assertEqual("DUAL", loaded.ai_provider)
            self.assertEqual("0.75", loaded.ai_min_confidence)
            self.assertEqual("openai-secret", loaded.openai_api_key)
            self.assertEqual("deepseek-secret", loaded.deepseek_api_key)
            self.assertEqual("qwen-secret", loaded.qwen_api_key)
            self.assertEqual("qwen-plus", loaded.qwen_model)
            self.assertTrue(loaded.openai_reasoning_enabled)
            self.assertEqual("high", loaded.openai_reasoning_effort)
            self.assertTrue(loaded.qwen_thinking_enabled)
            self.assertEqual("medium", loaded.qwen_reasoning_effort)
            self.assertTrue(loaded.deepseek_thinking_enabled)
            self.assertEqual("max", loaded.deepseek_reasoning_effort)
            self.assertEqual(90, loaded.ai_entry_timing_bars)
            self.assertEqual("binance-key", loaded.api_key)
            self.assertEqual("binance-secret", loaded.api_secret)
            self.assertEqual(1, loaded.leverage)
            content = path.read_text(encoding="utf-8")
            self.assertIn('"api_key": "binance-key"', content)
            self.assertIn('"api_secret": "binance-secret"', content)
            self.assertIn('"openai_api_key": "openai-secret"', content)
            self.assertIn('"deepseek_api_key": "deepseek-secret"', content)
            self.assertIn('"qwen_api_key": "qwen-secret"', content)

    def test_configured_credential_takes_priority_over_environment(self) -> None:
        with patch.dict("os.environ", {"BINANCE_API_KEY": "environment-key"}):
            self.assertEqual(
                "configured-key",
                credential_or_environment(" configured-key ", "BINANCE_API_KEY"),
            )
            self.assertEqual(
                "environment-key",
                credential_or_environment("", "BINANCE_API_KEY"),
            )

    def test_invalid_live_parameters_are_rejected(self) -> None:
        config = AppConfig(symbols=["AAPL"], trading_mode="REAL", buy_notional="0")
        with self.assertRaisesRegex(ValueError, "开仓金额"):
            config.validate()

    def test_invalid_manual_direction_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "手动方向"):
            AppConfig(
                symbols=["AAPL"],
                manual_directions={"AAPL": "UP"},
            ).validate()

    def test_non_finite_amount_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "开仓金额"):
            AppConfig(symbols=["AAPL"], buy_notional="Infinity").validate()

    def test_unicode_symbol_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "代码格式"):
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

    def test_legacy_contract_multiplier_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                '{"symbols":["AAPL"],"contract_multiplier":"5"}',
                encoding="utf-8",
            )
            store = ConfigStore(path)

            loaded = store.load()
            store.save(loaded)

            self.assertFalse(hasattr(loaded, "contract_multiplier"))
            self.assertNotIn("contract_multiplier", path.read_text(encoding="utf-8"))

    def test_symbol_count_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能超过"):
            AppConfig(symbols=[f"A{index}" for index in range(21)]).validate()

    def test_ai_settings_are_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "置信度"):
            AppConfig(symbols=["AAPL"], ai_min_confidence="0.4").validate()
        with self.assertRaisesRegex(ValueError, "大模型模式"):
            AppConfig(symbols=["AAPL"], ai_provider="unknown").validate()
        with self.assertRaisesRegex(ValueError, "K 线数量"):
            AppConfig(symbols=["AAPL"], ai_entry_timing_bars=9).validate()
        with self.assertRaisesRegex(ValueError, "推理强度"):
            AppConfig(
                symbols=["AAPL"], deepseek_reasoning_effort="ultra"
            ).validate()
        with self.assertRaisesRegex(ValueError, "OpenAI 推理强度"):
            AppConfig(
                symbols=["AAPL"], openai_reasoning_effort="ultra"
            ).validate()
        with self.assertRaisesRegex(ValueError, "Qwen 推理强度"):
            AppConfig(
                symbols=["AAPL"], qwen_reasoning_effort="ultra"
            ).validate()
        AppConfig(symbols=["AAPL"], ai_provider="qwen").validate()
        with self.assertRaisesRegex(ValueError, "Qwen 接口地址"):
            AppConfig(
                symbols=["AAPL"],
                qwen_chat_url="https://example.com/v1/chat/completions",
            ).validate()
        AppConfig(
            symbols=["AAPL"],
            qwen_chat_url=(
                "https://workspace-id.cn-beijing.maas.aliyuncs.com/"
                "compatible-mode/v1/chat/completions"
            ),
        ).validate()
        config = AppConfig(symbols=["AAPL"], ai_timeout_seconds=600)
        config.validate()
        self.assertEqual(600, config.ai_timeout_seconds)
        with self.assertRaisesRegex(ValueError, "超时"):
            AppConfig(symbols=["AAPL"], ai_timeout_seconds=601).validate()

    def test_futures_provider_and_leverage_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            store.save(
                AppConfig(
                    symbols=["BTCUSDT"],
                    provider="binance_futures",
                    leverage=10,
                )
            )

            loaded = store.load()

            self.assertEqual("binance_futures", loaded.provider)
            self.assertEqual(10, loaded.leverage)

    def test_leverage_must_be_between_one_and_125(self) -> None:
        with self.assertRaisesRegex(ValueError, "杠杆倍数"):
            AppConfig(symbols=["BTCUSDT"], leverage=0).validate()
        with self.assertRaisesRegex(ValueError, "杠杆倍数"):
            AppConfig(symbols=["BTCUSDT"], leverage=126).validate()


if __name__ == "__main__":
    unittest.main()

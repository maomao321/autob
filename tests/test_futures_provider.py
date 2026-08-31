from __future__ import annotations

import unittest
from decimal import Decimal

from autoquant_shared.models import OrderRequest, Side
from autoquant_backend.providers.binance_futures import BinanceFuturesProvider
from autoquant_backend.providers.binance_stocks import (
    OrderValidationError,
    ProviderTransportError,
)


def futures_info(
    symbol: str = "BTCUSDT", contract_type: str = "PERPETUAL"
) -> dict:
    return {
        "symbol": symbol,
        "status": "TRADING",
        "contractType": contract_type,
        "quoteAsset": "USDT",
        "filters": [
            {
                "filterType": "MARKET_LOT_SIZE",
                "minQty": "0.001",
                "maxQty": "100",
                "stepSize": "0.001",
            },
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }


class BinanceFuturesProviderTests(unittest.TestCase):
    def test_defaults_to_one_x_and_uses_current_market_stream_path(self) -> None:
        provider = BinanceFuturesProvider(include_daily_stream=True)

        self.assertEqual(1, provider.leverage)
        self.assertTrue(provider.supports_short)
        self.assertEqual(
            "wss://fstream.binance.com/market/stream?streams="
            "btcusdt@kline_5m/btcusdt@kline_1d",
            provider._stream_url("BTCUSDT"),
        )
        self.assertEqual(
            "wss://fstream.binance.com/market/stream?streams="
            "btcusdt@kline_5m/btcusdt@kline_1d/"
            "ethusdt@kline_5m/ethusdt@kline_1d",
            provider._combined_stream_url(["BTCUSDT", "ETHUSDT"]),
        )
        self.assertEqual(
            "wss://fstream.binance.com/private/ws/listen-key",
            provider._user_stream_url("listen-key"),
        )

    def test_leverage_is_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "1 到 125"):
            BinanceFuturesProvider(leverage=126)

    def test_symbol_validation_accepts_trading_perpetual_contract(self) -> None:
        class SymbolProvider(BinanceFuturesProvider):
            def _request_json(self, method, path, params, signed):
                self.request = (method, path, params, signed)
                return {"symbols": [futures_info()]}

        provider = SymbolProvider(leverage=3)
        result = provider.check_symbol("btcusdt")

        self.assertEqual("BUY_SELL", result["tradability"])
        self.assertIn("3x", result["validation"])
        self.assertEqual(("GET", "/fapi/v1/exchangeInfo", {}, False), provider.request)

    def test_symbol_validation_accepts_tradfi_perpetual_contract(self) -> None:
        class SymbolProvider(BinanceFuturesProvider):
            def _request_json(self, method, path, params, signed):
                return {
                    "symbols": [
                        futures_info("SOXLUSDT", "TRADIFI_PERPETUAL")
                    ]
                }

        result = SymbolProvider(leverage=3).check_symbol("soxlusdt")

        self.assertEqual("BUY_SELL", result["tradability"])
        self.assertIn("TradFi 永续合约校验通过", result["validation"])

    def test_symbol_validation_rejects_delivery_contract(self) -> None:
        class SymbolProvider(BinanceFuturesProvider):
            def _request_json(self, method, path, params, signed):
                return {
                    "symbols": [futures_info("BTCUSDT", "CURRENT_QUARTER")]
                }

        with self.assertRaisesRegex(RuntimeError, "不是 USDⓈ-M 永续合约"):
            SymbolProvider().check_symbol("BTCUSDT")

    def test_24h_rankings_filter_and_sort_active_usdt_perpetuals(self) -> None:
        class RankingProvider(BinanceFuturesProvider):
            def _request_json(self, method, path, params, signed):
                if path.endswith("exchangeInfo"):
                    inactive = futures_info("OLDUSDT")
                    inactive["status"] = "SETTLING"
                    usdc = futures_info("BTCUSDC")
                    usdc["quoteAsset"] = "USDC"
                    return {
                        "symbols": [
                            futures_info("BTCUSDT"),
                            futures_info("ETHUSDT"),
                            futures_info("SOLUSDT"),
                            futures_info("SOXLUSDT", "TRADIFI_PERPETUAL"),
                            futures_info("MSTRUSDT", "TRADIFI_PERPETUAL"),
                            futures_info("BTCUSDT_260925", "CURRENT_QUARTER"),
                            inactive,
                            usdc,
                        ]
                    }
                return [
                    {
                        "symbol": "ETHUSDT",
                        "priceChangePercent": "3.5",
                        "lastPrice": "3200",
                        "quoteVolume": "2000000",
                    },
                    {
                        "symbol": "SOLUSDT",
                        "priceChangePercent": "-8.2",
                        "lastPrice": "150",
                        "quoteVolume": "1000000",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "priceChangePercent": "7.1",
                        "lastPrice": "65000",
                        "quoteVolume": "9000000",
                    },
                    {
                        "symbol": "BTCUSDT_260925",
                        "priceChangePercent": "99",
                        "lastPrice": "66000",
                        "quoteVolume": "1",
                    },
                    {
                        "symbol": "SOXLUSDT",
                        "priceChangePercent": "12.5",
                        "lastPrice": "42",
                        "quoteVolume": "800000",
                    },
                    {
                        "symbol": "MSTRUSDT",
                        "priceChangePercent": "-3.2",
                        "lastPrice": "350",
                        "quoteVolume": "700000",
                    },
                ]

        result = RankingProvider().get_24h_rankings(limit=10)

        self.assertEqual(
            ["BTCUSDT", "ETHUSDT"],
            [item["symbol"] for item in result["crypto_gainers"]],
        )
        self.assertEqual(
            ["SOLUSDT"],
            [item["symbol"] for item in result["crypto_losers"]],
        )
        self.assertEqual(
            ["SOXLUSDT"],
            [item["symbol"] for item in result["stock_gainers"]],
        )
        self.assertEqual(
            ["MSTRUSDT"],
            [item["symbol"] for item in result["stock_losers"]],
        )
        self.assertEqual(
            "7.1", result["crypto_gainers"][0]["price_change_percent"]
        )
        self.assertEqual("stock", result["tickers"]["SOXLUSDT"]["market"])
        self.assertEqual("-8.2", result["tickers"]["SOLUSDT"]["price_change_percent"])
        self.assertNotIn("BTCUSDT_260925", result["tickers"])

    def test_live_buy_sets_leverage_and_converts_notional_to_quantity(self) -> None:
        class CapturingProvider(BinanceFuturesProvider):
            def __init__(self) -> None:
                super().__init__(
                    api_key="key", api_secret="secret", live_trading=True,
                    leverage=5,
                )
                self.calls: list[tuple[str, str, dict]] = []

            def check_symbol(self, symbol: str) -> dict:
                info = futures_info(symbol)
                self._symbol_info[symbol] = info
                return info

            def _ensure_server_time(self) -> None:
                return

            def _signed_request(self, method: str, path: str, params: dict) -> dict:
                self.calls.append((method, path, dict(params)))
                if path.endswith("positionSide/dual"):
                    return {"dualSidePosition": False}
                if path.endswith("leverage"):
                    return {"symbol": "BTCUSDT", "leverage": 5}
                return {"orderId": 123, "status": "NEW"}

        provider = CapturingProvider()
        result = provider.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=Side.BUY,
                reference_price=Decimal("50000"),
                buy_notional=Decimal("100"),
                sell_quantity=Decimal("0"),
                client_order_id="aq-futures-buy",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            {"symbol": "BTCUSDT", "leverage": 5}, provider.calls[1][2]
        )
        order_params = provider.calls[2][2]
        self.assertEqual("0.002", order_params["quantity"])
        self.assertEqual("aq-futures-buy", order_params["newClientOrderId"])
        self.assertNotIn("reduceOnly", order_params)

    def test_live_sell_is_reduce_only_and_rounded_down(self) -> None:
        class CapturingProvider(BinanceFuturesProvider):
            def __init__(self) -> None:
                super().__init__(api_key="key", api_secret="secret", live_trading=True)
                self._symbol_info["BTCUSDT"] = futures_info()
                self._prepared_symbols.add("BTCUSDT")
                self.params: dict = {}

            def _signed_request(self, method: str, path: str, params: dict) -> dict:
                self.params = dict(params)
                return {"orderId": 456, "status": "NEW"}

        provider = CapturingProvider()
        provider.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=Side.SELL,
                reference_price=Decimal("50000"),
                buy_notional=Decimal("0"),
                sell_quantity=Decimal("0.0019"),
                reduce_only=True,
            )
        )

        self.assertEqual("0.001", provider.params["quantity"])
        self.assertEqual("true", provider.params["reduceOnly"])

    def test_live_sell_can_open_short_without_reduce_only(self) -> None:
        class CapturingProvider(BinanceFuturesProvider):
            def __init__(self) -> None:
                super().__init__(api_key="key", api_secret="secret", live_trading=True)
                self._symbol_info["BTCUSDT"] = futures_info()
                self._prepared_symbols.add("BTCUSDT")
                self.params: dict = {}

            def _signed_request(self, method: str, path: str, params: dict) -> dict:
                self.params = dict(params)
                return {"orderId": 789, "status": "NEW"}

        provider = CapturingProvider()
        provider.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=Side.SELL,
                reference_price=Decimal("50000"),
                buy_notional=Decimal("100"),
                sell_quantity=Decimal("0"),
                allow_short=True,
            )
        )

        self.assertEqual("SELL", provider.params["side"])
        self.assertEqual("0.002", provider.params["quantity"])
        self.assertNotIn("reduceOnly", provider.params)

    def test_live_buy_can_reduce_short(self) -> None:
        class CapturingProvider(BinanceFuturesProvider):
            def __init__(self) -> None:
                super().__init__(api_key="key", api_secret="secret", live_trading=True)
                self._symbol_info["BTCUSDT"] = futures_info()
                self._prepared_symbols.add("BTCUSDT")
                self.params: dict = {}

            def _signed_request(self, method: str, path: str, params: dict) -> dict:
                self.params = dict(params)
                return {"orderId": 790, "status": "NEW"}

        provider = CapturingProvider()
        provider.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                side=Side.BUY,
                reference_price=Decimal("50000"),
                buy_notional=Decimal("0"),
                sell_quantity=Decimal("0.0019"),
                reduce_only=True,
            )
        )

        self.assertEqual("BUY", provider.params["side"])
        self.assertEqual("0.001", provider.params["quantity"])
        self.assertEqual("true", provider.params["reduceOnly"])

    def test_hedge_mode_is_rejected_before_leverage_or_order(self) -> None:
        class HedgeModeProvider(BinanceFuturesProvider):
            def __init__(self) -> None:
                super().__init__(api_key="key", api_secret="secret", live_trading=True)
                self._symbol_info["BTCUSDT"] = futures_info()

            def check_symbol(self, symbol: str) -> dict:
                return self._symbol_info[symbol]

            def _ensure_server_time(self) -> None:
                return

            def _signed_request(self, method: str, path: str, params: dict) -> dict:
                return {"dualSidePosition": True}

        provider = HedgeModeProvider()
        with self.assertRaisesRegex(OrderValidationError, "单向持仓模式"):
            provider.place_order(
                OrderRequest(
                    symbol="BTCUSDT", side=Side.BUY,
                    reference_price=Decimal("50000"),
                    buy_notional=Decimal("100"), sell_quantity=Decimal("0"),
                )
            )

    def test_preflight_transport_failure_is_known_before_order_submission(self) -> None:
        class OfflineProvider(BinanceFuturesProvider):
            def __init__(self) -> None:
                super().__init__(api_key="key", api_secret="secret", live_trading=True)

            def check_symbol(self, symbol: str) -> dict:
                self._symbol_info[symbol] = futures_info(symbol)
                return self._symbol_info[symbol]

            def _ensure_server_time(self) -> None:
                return

            def _signed_request(self, method: str, path: str, params: dict) -> dict:
                raise ProviderTransportError("offline")

        with self.assertRaisesRegex(OrderValidationError, "尚未发送订单"):
            OfflineProvider().place_order(
                OrderRequest(
                    symbol="BTCUSDT", side=Side.BUY,
                    reference_price=Decimal("50000"),
                    buy_notional=Decimal("100"), sell_quantity=Decimal("0"),
                )
            )

    def test_account_balance_returns_requested_margin_asset(self) -> None:
        class AccountProvider(BinanceFuturesProvider):
            def __init__(self) -> None:
                super().__init__(api_key="key", api_secret="secret")

            def _ensure_server_time(self) -> None:
                return

            def _signed_request_payload(self, method, path, params):
                self.request = (method, path, params)
                return [
                    {"asset": "USDT", "balance": "250.75"},
                    {"asset": "USDC", "balance": "10"},
                ]

        provider = AccountProvider()

        self.assertEqual(Decimal("250.75"), provider.get_account_total("usdt"))
        self.assertEqual(("GET", "/fapi/v3/balance", {}), provider.request)

    def test_historical_klines_are_parsed_and_closed(self) -> None:
        class HistoryProvider(BinanceFuturesProvider):
            def _request_json(self, method, path, params, signed):
                return [
                    [1000, "10", "12", "9", "11", "5", 1999],
                    [2000, "11", "13", "10", "12", "6", 2999],
                ]

        bars = HistoryProvider().get_historical_bars(
            "BTCUSDT", "5m", 1000, 2999, 2
        )

        self.assertEqual(2, len(bars))
        self.assertEqual(Decimal("12"), bars[-1].close)
        self.assertTrue(all(bar.closed for bar in bars))


if __name__ == "__main__":
    unittest.main()

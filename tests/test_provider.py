from __future__ import annotations

import unittest
from decimal import Decimal

from autoquant.models import OrderRequest, Side
from autoquant.providers.binance_stocks import (
    BinanceStocksProvider,
    OrderStatusUnknownError,
    OrderValidationError,
    ProviderHTTPError,
)


class BinanceStocksProviderTests(unittest.TestCase):
    def test_parse_combined_kline_message(self) -> None:
        payload = {
            "stream": "AAPL@kline_5m",
            "data": {
                "e": "kline",
                "E": 1710320400123,
                "s": "AAPL",
                "k": {
                    "t": 1710320100000,
                    "T": 1710320399999,
                    "s": "AAPL",
                    "i": "5m",
                    "o": "180.10",
                    "h": "181.20",
                    "l": "179.90",
                    "c": "181.00",
                    "v": "1234.5",
                    "x": True,
                },
            },
        }

        result = BinanceStocksProvider.parse_kline_message(payload)

        self.assertEqual("AAPL", result.symbol)
        self.assertEqual("5m", result.interval)
        self.assertEqual(Decimal("181.00"), result.close)
        self.assertTrue(result.closed)

    def test_hmac_signature_matches_known_value(self) -> None:
        provider = BinanceStocksProvider(api_secret="secret")
        self.assertEqual(
            "07dfcaff4deb862620f6ac3b94cf35682832e0ed9abffc5ebe6a52a00d0fb6bc",
            provider._signature("symbol=AAPL&timestamp=1"),
        )

    def test_paper_order_never_requires_credentials(self) -> None:
        provider = BinanceStocksProvider(live_trading=False)
        result = provider.place_order(
            OrderRequest(
                symbol="AAPL",
                side=Side.BUY,
                reference_price=Decimal("180"),
                buy_notional=Decimal("100"),
                sell_quantity=Decimal("1"),
            )
        )
        self.assertTrue(result.accepted)
        self.assertTrue(result.paper)
        self.assertTrue(result.order_id.startswith("paper-"))

    def test_live_market_order_uses_side_specific_size_field(self) -> None:
        class CapturingProvider(BinanceStocksProvider):
            def __init__(self) -> None:
                super().__init__(api_key="key", api_secret="secret", live_trading=True)
                self.params: list[dict] = []

            def _signed_request(self, method: str, path: str, params: dict) -> dict:
                self.params.append(dict(params))
                return {"status": "S", "orderId": "test-order"}

        provider = CapturingProvider()
        common = {
            "symbol": "AAPL",
            "reference_price": Decimal("180"),
            "buy_notional": Decimal("100.129"),
            "sell_quantity": Decimal("1.25"),
            "client_order_id": "aq-known-id",
        }
        provider.place_order(OrderRequest(side=Side.BUY, **common))
        provider.place_order(OrderRequest(side=Side.SELL, **common))

        self.assertEqual("100.12", provider.params[0]["notional"])
        self.assertEqual("aq-known-id", provider.params[0]["clientOrderId"])
        self.assertNotIn("quantity", provider.params[0])
        self.assertEqual("1.25", provider.params[1]["quantity"])
        self.assertNotIn("notional", provider.params[1])

    def test_live_order_applies_exchange_size_filters(self) -> None:
        class CapturingProvider(BinanceStocksProvider):
            def __init__(self) -> None:
                super().__init__(api_key="key", api_secret="secret", live_trading=True)
                self.params: dict = {}

            def _signed_request(self, method: str, path: str, params: dict) -> dict:
                self.params = dict(params)
                return {"status": "S", "orderId": "test-order"}

        provider = CapturingProvider()
        provider._symbol_info["AAPL"] = {
            "filters": [
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.50",
                    "maxQty": "10",
                    "stepSize": "0.50",
                },
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "minQty": "0.10",
                    "maxQty": "10",
                    "stepSize": "0.05",
                },
                {
                    "filterType": "NOTIONAL",
                    "minNotional": "10",
                    "maxNotional": "10000",
                },
            ]
        }

        provider.place_order(
            OrderRequest(
                symbol="AAPL",
                side=Side.SELL,
                reference_price=Decimal("180"),
                buy_notional=Decimal("100"),
                sell_quantity=Decimal("1.27"),
            )
        )

        self.assertEqual("1.25", provider.params["quantity"])

    def test_live_order_rejects_amount_below_exchange_minimum(self) -> None:
        provider = BinanceStocksProvider(
            api_key="key", api_secret="secret", live_trading=True
        )
        provider._symbol_info["AAPL"] = {
            "filters": [
                {"filterType": "NOTIONAL", "minNotional": "10"},
            ]
        }

        with self.assertRaises(OrderValidationError):
            provider.place_order(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.BUY,
                    reference_price=Decimal("180"),
                    buy_notional=Decimal("9.99"),
                    sell_quantity=Decimal("1"),
                )
            )

    def test_binance_timeout_error_is_treated_as_unknown(self) -> None:
        class TimeoutProvider(BinanceStocksProvider):
            def _signed_request(self, method: str, path: str, params: dict) -> dict:
                raise ProviderHTTPError(400, "timeout", -1007)

        provider = TimeoutProvider(
            api_key="key", api_secret="secret", live_trading=True
        )
        with self.assertRaises(OrderStatusUnknownError):
            provider.place_order(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.BUY,
                    reference_price=Decimal("180"),
                    buy_notional=Decimal("100"),
                    sell_quantity=Decimal("1"),
                )
            )

    def test_success_response_without_order_id_is_unknown(self) -> None:
        class MissingIdProvider(BinanceStocksProvider):
            def _signed_request(self, method: str, path: str, params: dict) -> dict:
                return {"status": "S"}

        provider = MissingIdProvider(
            api_key="key", api_secret="secret", live_trading=True
        )
        with self.assertRaises(OrderStatusUnknownError):
            provider.place_order(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.BUY,
                    reference_price=Decimal("180"),
                    buy_notional=Decimal("100"),
                    sell_quantity=Decimal("1"),
                )
            )

    def test_account_total_sums_active_wallets_in_usdc(self) -> None:
        class AccountProvider(BinanceStocksProvider):
            def __init__(self) -> None:
                super().__init__(api_key="key", api_secret="secret")
                self.params: dict = {}

            def _ensure_server_time(self) -> None:
                return

            def _signed_request_payload(
                self, method: str, path: str, params: dict
            ) -> list[dict]:
                self.params = dict(params)
                return [
                    {"activate": True, "balance": "100.50", "walletName": "Spot"},
                    {"activate": True, "balance": "2.25", "walletName": "Funding"},
                    {"activate": False, "balance": "50", "walletName": "Unused"},
                ]

        provider = AccountProvider()

        self.assertEqual(Decimal("102.75"), provider.get_account_total("usdc"))
        self.assertEqual("USDC", provider.params["quoteAsset"])

    def test_latest_price_uses_bid_ask_midpoint(self) -> None:
        class QuoteProvider(BinanceStocksProvider):
            def _request_json(
                self, method: str, path: str, params: dict, signed: bool
            ) -> dict:
                return {
                    "symbol": "AAPL",
                    "bidPrice": "180.50",
                    "askPrice": "180.54",
                }

        provider = QuoteProvider(api_key="key", api_secret="secret")

        self.assertEqual(Decimal("180.52"), provider.get_latest_price("AAPL"))


if __name__ == "__main__":
    unittest.main()

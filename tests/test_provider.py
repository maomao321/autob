from __future__ import annotations

import unittest
from decimal import Decimal

from autoquant.models import OrderRequest, Side
from autoquant.providers.binance_stocks import BinanceStocksProvider


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
        }
        provider.place_order(OrderRequest(side=Side.BUY, **common))
        provider.place_order(OrderRequest(side=Side.SELL, **common))

        self.assertEqual("100.12", provider.params[0]["notional"])
        self.assertNotIn("quantity", provider.params[0])
        self.assertEqual("1.25", provider.params[1]["quantity"])
        self.assertNotIn("notional", provider.params[1])


if __name__ == "__main__":
    unittest.main()

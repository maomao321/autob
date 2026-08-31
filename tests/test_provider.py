from __future__ import annotations

from io import BytesIO
import unittest
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

from autoquant_shared.models import OrderRequest, Side
from autoquant_backend.providers.binance_stocks import (
    BinanceStocksProvider,
    OrderStatusUnknownError,
    OrderValidationError,
    ProviderHTTPError,
)


class BinanceStocksProviderTests(unittest.TestCase):
    def test_parse_nasdaq_daily_history_returns_chronological_ohlc(self) -> None:
        payload = {
            "data": {
                "tradesTable": {
                    "rows": [
                        {
                            "date": "08/19/2026",
                            "close": "$102.50",
                            "open": "$101.00",
                            "high": "$103.00",
                            "low": "$100.50",
                            "volume": "1,200",
                        },
                        {
                            "date": "08/18/2026",
                            "close": "$100.00",
                            "open": "$99.00",
                            "high": "$101.00",
                            "low": "$98.50",
                            "volume": "1,000",
                        },
                    ]
                }
            }
        }

        bars = BinanceStocksProvider.parse_nasdaq_daily_bars(payload, "AAPL")

        self.assertEqual(2, len(bars))
        self.assertLess(bars[0].open_time, bars[1].open_time)
        self.assertEqual(Decimal("100.00"), bars[0].close)
        self.assertEqual(Decimal("102.50"), bars[1].close)
        self.assertTrue(all(bar.interval == "1d" for bar in bars))
        self.assertTrue(all(bar.closed for bar in bars))

    def test_parse_nasdaq_points_aggregates_five_minute_ohlc(self) -> None:
        payload = {
            "data": {
                "chart": [
                    {"x": 1_020_000, "y": "100", "w": "10"},
                    {"x": 1_080_000, "y": "102", "w": "20"},
                    {"x": 1_140_000, "y": "99", "w": "30"},
                    {"x": 1_320_000, "y": "103", "w": "40"},
                    {"x": 1_380_000, "y": None, "w": "50"},
                ]
            }
        }

        bars = BinanceStocksProvider.parse_nasdaq_chart_bars(payload, "AAPL")

        self.assertEqual(2, len(bars))
        self.assertEqual(900_000, bars[0].open_time)
        self.assertEqual(1_199_999, bars[0].close_time)
        self.assertEqual(Decimal("100"), bars[0].open)
        self.assertEqual(Decimal("102"), bars[0].high)
        self.assertEqual(Decimal("99"), bars[0].low)
        self.assertEqual(Decimal("99"), bars[0].close)
        self.assertEqual(Decimal("60"), bars[0].volume)
        self.assertEqual(Decimal("103"), bars[1].close)
        self.assertTrue(all(bar.closed for bar in bars))

    def test_historical_bars_are_filtered_and_limited(self) -> None:
        class HistoryProvider(BinanceStocksProvider):
            def _request_public_json(self, url: str, **_kwargs) -> dict:
                return {
                    "data": {
                        "chart": [
                            {"x": 1_020_000, "y": "100", "w": "10"},
                            {"x": 1_320_000, "y": "101", "w": "20"},
                            {"x": 1_620_000, "y": "102", "w": "30"},
                        ]
                    }
                }

        provider = HistoryProvider()
        bars = provider.get_historical_bars(
            "AAPL", "5m", 900_000, 1_799_999, 1
        )

        self.assertEqual(1, len(bars))
        self.assertEqual(1_500_000, bars[0].open_time)

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

    def test_parse_current_kline_close_time_field(self) -> None:
        payload = {
            "stream": "DRAM@kline_1d",
            "data": {
                "e": "kline",
                "E": 1776677041000,
                "s": "DRAM",
                "k": {
                    "t": 1776643200000,
                    "ct": 1776729599999,
                    "s": "DRAM",
                    "i": "1d",
                    "o": "12.10",
                    "h": "12.40",
                    "l": "12.00",
                    "c": "12.30",
                    "v": "1000",
                    "x": False,
                },
            },
        }

        result = BinanceStocksProvider.parse_kline_message(payload)

        self.assertEqual(1776729599999, result.close_time)
        self.assertEqual("1d", result.interval)
        self.assertEqual("DRAM", result.symbol)

    def test_hmac_signature_matches_known_value(self) -> None:
        provider = BinanceStocksProvider(api_secret="secret")
        self.assertEqual(
            "07dfcaff4deb862620f6ac3b94cf35682832e0ed9abffc5ebe6a52a00d0fb6bc",
            provider._signature("symbol=AAPL&timestamp=1"),
        )

    def test_signed_request_refreshes_server_time_at_common_entrypoint(self) -> None:
        class SignedProvider(BinanceStocksProvider):
            def __init__(self) -> None:
                super().__init__(api_key="key", api_secret="secret")
                self.ensure_calls = 0

            def _ensure_server_time(self) -> None:
                self.ensure_calls += 1

            def _request_json(self, method, path, params, signed):
                self.request = (method, path, params, signed)
                return {"ok": True}

        provider = SignedProvider()

        self.assertEqual(
            {"ok": True}, provider._signed_request("GET", "/signed", {})
        )
        self.assertEqual(1, provider.ensure_calls)
        self.assertTrue(provider.request[3])

    def test_binance_get_retries_rate_limit_using_retry_after(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read() -> bytes:
                return b'{"ok": true}'

        error = HTTPError(
            "https://api.binance.com/test",
            429,
            "rate limited",
            {"Retry-After": "0"},
            BytesIO(b'{"code": -1003, "msg": "too many requests"}'),
        )
        provider = BinanceStocksProvider()
        with (
            patch(
                "autoquant_backend.providers.binance_stocks.urlopen",
                side_effect=[error, Response()],
            ) as urlopen_mock,
            patch.object(provider, "_wait_for_request_slot"),
            patch("autoquant_backend.providers.binance_stocks.time.sleep"),
        ):
            payload = provider._request_public_json(
                "https://api.binance.com/test", source_name="Binance"
            )

        self.assertEqual({"ok": True}, payload)
        self.assertEqual(2, urlopen_mock.call_count)

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

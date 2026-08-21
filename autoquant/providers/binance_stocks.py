from __future__ import annotations

import hashlib
import hmac
import json
import queue
import ssl
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from threading import Event
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from autoquant.models import Bar, OrderRequest, OrderResult, Side
from autoquant.providers.base import StatusCallback, TradingProvider


class ProviderError(RuntimeError):
    """A safe, user-facing provider failure."""


class OrderValidationError(ProviderError):
    """The order is invalid and was not sent."""


class ProviderHTTPError(ProviderError):
    def __init__(
        self,
        status_code: int,
        message: str,
        exchange_code: int | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.exchange_code = exchange_code


class ProviderTransportError(ProviderError):
    """No conclusive response was received from the exchange."""


class OrderRejectedError(ProviderError):
    """The exchange conclusively rejected the order."""


class OrderStatusUnknownError(ProviderError):
    """The order may have reached the exchange, so its state is unknown."""


class BinanceStocksProvider(TradingProvider):
    name = "binance_stocks"
    supports_short = False
    _public_cache_lock = threading.RLock()
    _tokenized_assets_cache: tuple[float, list[dict[str, Any]]] | None = None
    _request_semaphore = threading.BoundedSemaphore(4)
    _nasdaq_quote_base_url = "https://api.nasdaq.com/api/quote"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        live_trading: bool = False,
        rest_base_url: str = "https://api.binance.com",
        websocket_base_url: str = "wss://nbstream.binance.com/equity",
        recv_window: int = 5000,
        request_timeout: float = 10.0,
        include_daily_stream: bool = True,
    ) -> None:
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.live_trading = live_trading
        self.rest_base_url = rest_base_url.rstrip("/")
        self.websocket_base_url = websocket_base_url.rstrip("/")
        self.recv_window = recv_window
        self.request_timeout = request_timeout
        self.include_daily_stream = include_daily_stream
        self._server_time_offset_ms = 0
        self._symbol_info: dict[str, dict[str, Any]] = {}
        self._symbol_info_cached_at: dict[str, float] = {}
        self._latest_price_cache: dict[str, tuple[float, Decimal]] = {}
        self._server_time_synced_at = 0.0

    def stream_bars(
        self,
        symbol: str,
        stop_event: Event,
        status_callback: StatusCallback | None = None,
    ) -> Iterator[Bar]:
        try:
            import websocket
        except ImportError as exc:
            raise ProviderError(
                "缺少 websocket-client，请先执行: python -m pip install -e ."
            ) from exc

        symbol = symbol.upper()
        streams = f"{symbol}@kline_5m"
        if self.include_daily_stream:
            streams += f"/{symbol}@kline_1d"
        url = f"{self.websocket_base_url}/stream?streams={streams}"
        reconnect_delay = 1.0

        while not stop_event.is_set():
            messages: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=512)

            def put_message(item: tuple[str, Any]) -> None:
                try:
                    messages.put_nowait(item)
                except queue.Full:
                    try:
                        messages.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        messages.put_nowait(item)
                    except queue.Full:
                        pass

            def on_open(_ws: Any) -> None:
                put_message(("status", "行情 WebSocket 已连接"))

            def on_message(_ws: Any, raw_message: str) -> None:
                nonlocal reconnect_delay
                reconnect_delay = 1.0
                try:
                    payload = json.loads(raw_message)
                    put_message(("bar", self.parse_kline_message(payload)))
                except (TypeError, ValueError, KeyError) as exc:
                    put_message(("status", f"忽略无法解析的行情消息: {exc}"))

            def on_error(_ws: Any, error: Any) -> None:
                put_message(("status", f"行情连接错误: {error}"))

            def on_close(
                _ws: Any,
                status_code: int | None,
                close_message: str | None,
            ) -> None:
                detail = close_message or "连接已关闭"
                put_message(("status", f"行情连接关闭({status_code}): {detail}"))

            ws = websocket.WebSocketApp(
                url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            socket_thread = threading.Thread(
                target=ws.run_forever,
                kwargs={
                    "ping_interval": 20,
                    "ping_timeout": 10,
                    "sslopt": {"cert_reqs": ssl.CERT_REQUIRED},
                },
                name=f"binance-ws-{symbol}",
                daemon=True,
            )
            socket_thread.start()

            while (
                socket_thread.is_alive() or not messages.empty()
            ) and not stop_event.is_set():
                try:
                    kind, value = messages.get(timeout=0.5)
                except queue.Empty:
                    continue
                if kind == "bar":
                    yield value
                elif status_callback:
                    status_callback(str(value))

            ws.close()
            socket_thread.join(timeout=2)
            if stop_event.is_set():
                break
            if status_callback:
                status_callback(f"{reconnect_delay:.0f} 秒后重连行情")
            if stop_event.wait(reconnect_delay):
                break
            reconnect_delay = min(reconnect_delay * 2, 30.0)

    def place_order(self, order: OrderRequest) -> OrderResult:
        if not self.live_trading:
            order_id = f"paper-{uuid.uuid4().hex}"
            size = (
                f"金额 {order.buy_notional} USDC"
                if order.side is Side.BUY
                else f"数量 {order.sell_quantity}"
            )
            return OrderResult(
                accepted=True,
                order_id=order_id,
                message=f"模拟 {order.side.value} 已记录，{size}",
                paper=True,
                raw={"status": "S", "orderId": order_id},
            )

        self._require_credentials()
        client_order_id = order.client_order_id or f"aq{uuid.uuid4().hex}"
        params: dict[str, Any] = {
            "symbol": order.symbol.upper(),
            "side": order.side.value,
            "orderType": "MARKET",
            "clientOrderId": client_order_id,
        }
        try:
            params.update(self._format_order_size(order))
        except OrderValidationError:
            raise
        except (ArithmeticError, ValueError) as exc:
            raise OrderValidationError(f"订单数量格式无效: {exc}") from exc

        try:
            payload = self._signed_request(
                "POST", "/sapi/v1/equity/order/place", params
            )
        except ProviderHTTPError as exc:
            execution_unknown = exc.exchange_code in {-1006, -1007}
            if 400 <= exc.status_code < 500 and not execution_unknown:
                raise OrderRejectedError(str(exc)) from exc
            raise OrderStatusUnknownError(str(exc)) from exc
        except ProviderTransportError as exc:
            raise OrderStatusUnknownError(str(exc)) from exc
        accepted = payload.get("status") == "S"
        order_id = str(payload.get("orderId", ""))
        if accepted and not order_id:
            raise OrderStatusUnknownError(
                "Binance 已确认接收订单，但响应缺少 orderId"
            )
        return OrderResult(
            accepted=accepted,
            order_id=order_id,
            message=("实盘订单已接受" if accepted else "实盘订单被拒绝"),
            paper=False,
            raw=payload,
        )

    def check_symbol(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if not self.api_key:
            if self.live_trading:
                raise ProviderError("实盘模式必须填写 API Key")
            return {
                "symbol": symbol,
                "tradability": "UNKNOWN",
                "validation": "模拟模式未提供 API Key，跳过交易所代码校验",
            }
        cached = self._symbol_info.get(symbol)
        cached_at = self._symbol_info_cached_at.get(symbol, 0.0)
        if cached is not None and time.monotonic() - cached_at < 60:
            return dict(cached)
        if self.live_trading and time.monotonic() - self._server_time_synced_at >= 60:
            self.sync_server_time()
        payload = self._request_json(
            "GET",
            "/sapi/v1/equity/market/exchangeInfo",
            {"symbol": symbol},
            signed=False,
        )
        if not isinstance(payload, dict):
            raise ProviderError("Binance 股票信息返回结构不符合预期")
        symbols = payload.get("symbols", [])
        if not symbols:
            raise ProviderError(f"Binance Stocks 不支持股票代码 {symbol}")
        info = dict(symbols[0])
        if self.live_trading:
            tokenized_assets = self._cached_tokenized_assets()
            supported = any(
                str(asset.get("underlyingEquitySymbol", "")).upper()
                == symbol
                and self._as_bool(asset.get("multiplierValid", False))
                for asset in tokenized_assets
                if isinstance(asset, dict)
            )
            if not supported:
                raise ProviderError(f"{symbol} 当前未启用 Binance 股票交易")
            info["tokenizationEnabled"] = True
        self._symbol_info[symbol] = info
        self._symbol_info_cached_at[symbol] = time.monotonic()
        return info

    def _cached_tokenized_assets(self) -> list[dict[str, Any]]:
        with self._public_cache_lock:
            cached = self.__class__._tokenized_assets_cache
            if cached is not None and time.monotonic() - cached[0] < 60:
                return cached[1]
            payload = self._request_json(
                "GET",
                "/sapi/v1/equity/market/tokenized-assets",
                {},
                signed=False,
            )
            if not isinstance(payload, list):
                raise ProviderError("Binance 代币化股票列表返回结构不符合预期")
            assets = [dict(item) for item in payload if isinstance(item, dict)]
            self.__class__._tokenized_assets_cache = (time.monotonic(), assets)
            return assets

    def get_order_detail(self, order_id: str) -> dict:
        self._require_credentials()
        payload = self._signed_request(
            "GET",
            "/sapi/v1/equity/order/detail",
            {"orderId": order_id},
        )
        return payload

    def get_account_total(self, quote_asset: str = "USDC") -> Decimal:
        self._require_credentials()
        self._ensure_server_time()
        normalized_quote = quote_asset.strip().upper()
        if (
            not 2 <= len(normalized_quote) <= 12
            or not normalized_quote.isascii()
            or not normalized_quote.isalnum()
        ):
            raise ProviderError("账户折算资产格式无效")
        payload = self._signed_request_payload(
            "GET",
            "/sapi/v1/asset/wallet/balance",
            {"quoteAsset": normalized_quote},
        )
        if not isinstance(payload, list):
            raise ProviderError("Binance 钱包余额返回结构不符合预期")
        total = Decimal("0")
        for item in payload:
            if not isinstance(item, dict) or not self._as_bool(
                item.get("activate", True)
            ):
                continue
            try:
                balance = Decimal(str(item["balance"]))
            except (KeyError, ArithmeticError, ValueError) as exc:
                raise ProviderError("Binance 钱包余额包含无效金额") from exc
            if not balance.is_finite() or balance < 0:
                raise ProviderError("Binance 钱包余额包含无效金额")
            total += balance
        return total

    def get_latest_price(self, symbol: str) -> Decimal:
        self._require_credentials()
        symbol = symbol.strip().upper()
        cached = self._latest_price_cache.get(symbol)
        if cached is not None and time.monotonic() - cached[0] < 5:
            return cached[1]
        payload = self._request_json(
            "GET",
            "/sapi/v1/equity/market/quote",
            {"symbol": symbol},
            signed=False,
        )
        if not isinstance(payload, dict) or not payload:
            raise ProviderError(f"{symbol} 当前没有可用报价")
        prices: list[Decimal] = []
        for field in ("bidPrice", "askPrice"):
            try:
                value = Decimal(str(payload.get(field, "0")))
            except (ArithmeticError, ValueError):
                continue
            if value.is_finite() and value > 0:
                prices.append(value)
        if not prices:
            raise ProviderError(f"{symbol} 当前没有有效买卖报价")
        result = sum(prices, Decimal("0")) / Decimal(len(prices))
        self._latest_price_cache[symbol] = (time.monotonic(), result)
        return result

    def get_historical_bars(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        limit: int,
    ) -> list[Bar]:
        if interval not in {"1d", "5m"}:
            raise ProviderError(f"历史预热暂不支持周期 {interval}")
        if limit <= 0 or end_time < start_time:
            return []
        normalized_symbol = symbol.strip().upper()
        bars: list[Bar] = []
        for asset_class in ("stocks", "etf"):
            if interval == "1d":
                params = urlencode(
                    {
                        "assetclass": asset_class,
                        "fromdate": self._utc_date(start_time),
                        "todate": self._utc_date(end_time),
                        "limit": max(limit, 10),
                    }
                )
                url = (
                    f"{self._nasdaq_quote_base_url}/{quote(normalized_symbol)}"
                    f"/historical?{params}"
                )
            else:
                params = urlencode(
                    {"assetclass": asset_class, "charttype": "rs"}
                )
                url = (
                    f"{self._nasdaq_quote_base_url}/{quote(normalized_symbol)}"
                    f"/chart?{params}"
                )
            payload = self._request_public_json(
                url, source_name="Nasdaq 历史行情"
            )
            bars = (
                self.parse_nasdaq_daily_bars(payload, normalized_symbol)
                if interval == "1d"
                else self.parse_nasdaq_chart_bars(
                    payload, normalized_symbol, interval
                )
            )
            if bars:
                break
        eligible = [
            bar
            for bar in bars
            if start_time <= bar.open_time and bar.close_time <= end_time
        ]
        return eligible[-limit:]

    @staticmethod
    def _utc_date(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(
            timestamp_ms / 1000, tz=timezone.utc
        ).date().isoformat()

    def _ensure_server_time(self) -> None:
        if time.monotonic() - self._server_time_synced_at >= 60:
            self.sync_server_time()

    def sync_server_time(self) -> int:
        payload = self._request_json("GET", "/api/v3/time", {}, signed=False)
        if not isinstance(payload, dict):
            raise ProviderError("Binance 时间接口返回结构不符合预期")
        server_time = int(payload["serverTime"])
        self._server_time_offset_ms = server_time - int(time.time() * 1000)
        self._server_time_synced_at = time.monotonic()
        return self._server_time_offset_ms

    @classmethod
    def parse_kline_message(cls, payload: dict[str, Any]) -> Bar:
        data = payload.get("data", payload)
        if data.get("e") != "kline":
            raise ValueError(f"不是 K 线事件: {data.get('e')!r}")
        kline = data.get("k", data)

        def required(*names: str) -> Any:
            for name in names:
                if name in kline:
                    return kline[name]
            raise KeyError("/".join(names))

        def optional(default: Any, *names: str) -> Any:
            for name in names:
                if name in kline:
                    return kline[name]
            return default

        return Bar(
            symbol=str(optional(data.get("s", ""), "s", "symbol")).upper(),
            interval=str(required("i", "interval")),
            open_time=int(required("t", "openTime", "startTime")),
            close_time=int(required("ct", "T", "closeTime", "endTime")),
            open=Decimal(str(required("o", "open"))),
            high=Decimal(str(required("h", "high"))),
            low=Decimal(str(required("l", "low"))),
            close=Decimal(str(required("c", "close"))),
            volume=Decimal(str(optional("0", "v", "volume"))),
            closed=cls._as_bool(optional(False, "x", "closed", "isClosed")),
            event_time=int(data.get("E", data.get("eventTime", 0))),
        )

    @staticmethod
    def parse_nasdaq_chart_bars(
        payload: dict[str, Any], symbol: str, interval: str = "5m"
    ) -> list[Bar]:
        try:
            data = payload.get("data")
            if data is None:
                return []
            chart = data.get("chart", [])
        except (AttributeError, TypeError) as exc:
            raise ProviderError("Nasdaq 历史行情返回结构不符合预期") from exc
        if not isinstance(chart, list):
            raise ProviderError("Nasdaq 历史行情返回结构不符合预期")

        interval_ms = {"5m": 300_000}.get(interval)
        if interval_ms is None:
            raise ProviderError(f"无法解析历史 K 线周期 {interval}")
        points: list[tuple[int, Decimal, Decimal]] = []
        for item in chart:
            if not isinstance(item, dict):
                continue
            try:
                timestamp = int(item["x"])
                price = Decimal(str(item["y"]))
                volume = Decimal(str(item.get("w", 0) or 0))
                if not price.is_finite() or price <= 0:
                    continue
                if not volume.is_finite() or volume < 0:
                    volume = Decimal("0")
                points.append((timestamp, price, volume))
            except (ArithmeticError, KeyError, TypeError, ValueError):
                continue
        points.sort(key=lambda point: point[0])

        buckets: dict[int, dict[str, Decimal]] = {}
        for timestamp, price, volume in points:
            open_time = timestamp - timestamp % interval_ms
            bucket = buckets.get(open_time)
            if bucket is None:
                buckets[open_time] = {
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume,
                }
                continue
            bucket["high"] = max(bucket["high"], price)
            bucket["low"] = min(bucket["low"], price)
            bucket["close"] = price
            bucket["volume"] += volume

        return [
            Bar(
                symbol=symbol.upper(),
                interval=interval,
                open_time=open_time,
                close_time=open_time + interval_ms - 1,
                open=values["open"],
                high=values["high"],
                low=values["low"],
                close=values["close"],
                volume=values["volume"],
                closed=True,
            )
            for open_time, values in sorted(buckets.items())
        ]

    @staticmethod
    def parse_nasdaq_daily_bars(
        payload: dict[str, Any], symbol: str
    ) -> list[Bar]:
        try:
            data = payload.get("data")
            if data is None:
                return []
            table = data.get("tradesTable")
            rows = table.get("rows") if isinstance(table, dict) else None
        except (AttributeError, TypeError) as exc:
            raise ProviderError("Nasdaq 历史日线返回结构不符合预期") from exc
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise ProviderError("Nasdaq 历史日线 rows 格式错误")

        bars: list[Bar] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                day = datetime.strptime(str(row.get("date", "")), "%m/%d/%Y")
                open_time = int(day.replace(tzinfo=timezone.utc).timestamp() * 1000)
                values = {
                    name: BinanceStocksProvider._market_decimal(row.get(name))
                    for name in ("open", "high", "low", "close")
                }
                if any(
                    value is None or value <= 0 for value in values.values()
                ):
                    continue
                volume = BinanceStocksProvider._market_decimal(row.get("volume"))
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        interval="1d",
                        open_time=open_time,
                        close_time=open_time + 86_400_000 - 1,
                        open=values["open"],
                        high=values["high"],
                        low=values["low"],
                        close=values["close"],
                        volume=volume or Decimal("0"),
                        closed=True,
                    )
                )
            except (ArithmeticError, TypeError, ValueError):
                continue
        bars.sort(key=lambda bar: bar.open_time)
        return bars

    @staticmethod
    def _market_decimal(value: Any) -> Decimal | None:
        try:
            parsed = Decimal(
                str(value).replace("$", "").replace(",", "").strip()
            )
        except (ArithmeticError, ValueError):
            return None
        return parsed if parsed.is_finite() and parsed >= 0 else None

    def _signed_request(
        self, method: str, path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        payload = self._signed_request_payload(method, path, params)
        if not isinstance(payload, dict):
            raise ProviderError("Binance 交易接口返回结构不符合预期")
        return payload

    def _signed_request_payload(
        self, method: str, path: str, params: dict[str, Any]
    ) -> Any:
        self._require_credentials()
        signed_params = dict(params)
        signed_params["recvWindow"] = self.recv_window
        signed_params["timestamp"] = (
            int(time.time() * 1000) + self._server_time_offset_ms
        )
        query = urlencode(signed_params)
        signed_params["signature"] = self._signature(query)
        return self._request_json(method, path, signed_params, signed=True)

    def _request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        signed: bool,
    ) -> Any:
        query = urlencode(params)
        url = f"{self.rest_base_url}{path}"
        if query:
            url = f"{url}?{query}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "AutoQuant/0.5.0",
        }
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        return self._request_public_json(
            url,
            method=method,
            headers=headers,
            source_name="Binance",
        )

    def _request_public_json(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        source_name: str,
    ) -> Any:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "AutoQuant/0.5.0",
        }
        if headers:
            request_headers.update(headers)
        request = Request(url, method=method.upper(), headers=request_headers)
        try:
            with self._request_semaphore:
                with urlopen(request, timeout=self.request_timeout) as response:
                    body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if source_name != "Binance":
                detail = body[:300] if body.strip() else str(exc.reason)
                raise ProviderError(
                    f"{source_name} HTTP {exc.code}: {detail}"
                ) from exc
            raise ProviderHTTPError(
                exc.code,
                self._error_message(exc.code, body),
                self._exchange_error_code(body),
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderTransportError(f"{source_name}请求失败: {exc}") from exc
        if not body.strip():
            return {}
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{source_name}返回了无效 JSON") from exc
        return payload

    def _signature(self, query: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _require_credentials(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ProviderError("实盘交易必须填写 API Key 和 API Secret")

    def _format_order_size(self, order: OrderRequest) -> dict[str, str]:
        info = self._symbol_info.get(order.symbol.upper(), {})
        if order.side is Side.BUY:
            notional = order.buy_notional.quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
            self._require_positive_finite(notional, "买入金额")
            self._validate_range(
                notional,
                self._rule(info, "minNotional", "NOTIONAL", "MIN_NOTIONAL"),
                self._rule(info, "maxNotional", "NOTIONAL"),
                "买入金额",
            )
            return {"notional": self._decimal_string(notional)}

        quantity = order.sell_quantity
        self._require_positive_finite(quantity, "卖出数量")
        step_size = self._rule(
            info, "stepSize", "MARKET_LOT_SIZE", "LOT_SIZE"
        )
        if step_size is not None and step_size > 0:
            quantity = (
                (quantity / step_size).to_integral_value(rounding=ROUND_DOWN)
                * step_size
            )
            if quantity <= 0:
                raise OrderValidationError(f"卖出数量小于最小步长 {step_size}")
        self._validate_range(
            quantity,
            self._rule(info, "minQty", "MARKET_LOT_SIZE", "LOT_SIZE"),
            self._rule(info, "maxQty", "MARKET_LOT_SIZE", "LOT_SIZE"),
            "卖出数量",
        )
        estimated_notional = quantity * order.reference_price
        self._validate_range(
            estimated_notional,
            self._rule(info, "minNotional", "NOTIONAL", "MIN_NOTIONAL"),
            self._rule(info, "maxNotional", "NOTIONAL"),
            "预计卖出金额",
        )
        return {"quantity": self._decimal_string(quantity)}

    @staticmethod
    def _rule(
        info: dict[str, Any],
        field: str,
        *filter_types: str,
    ) -> Decimal | None:
        value = info.get(field)
        if value not in (None, ""):
            try:
                parsed = Decimal(str(value))
                return parsed if parsed.is_finite() else None
            except (ArithmeticError, ValueError):
                return None
        filters = info.get("filters", [])
        for filter_type in filter_types:
            for item in filters:
                if not isinstance(item, dict):
                    continue
                if (
                    str(item.get("filterType", "")).upper()
                    != filter_type.upper()
                ):
                    continue
                value = item.get(field)
                if value in (None, ""):
                    continue
                try:
                    parsed = Decimal(str(value))
                    return parsed if parsed.is_finite() else None
                except (ArithmeticError, ValueError):
                    return None
        return None

    @staticmethod
    def _require_positive_finite(value: Decimal, label: str) -> None:
        if not value.is_finite() or value <= 0:
            raise OrderValidationError(f"{label}必须是有限正数")

    @staticmethod
    def _validate_range(
        value: Decimal,
        minimum: Decimal | None,
        maximum: Decimal | None,
        label: str,
    ) -> None:
        if minimum is not None and minimum > 0 and value < minimum:
            raise OrderValidationError(
                f"{label} {value} 小于交易所最小值 {minimum}"
            )
        if maximum is not None and maximum > 0 and value > maximum:
            raise OrderValidationError(
                f"{label} {value} 大于交易所最大值 {maximum}"
            )

    @staticmethod
    def _decimal_string(value: Decimal) -> str:
        result = format(value, "f")
        if "." in result:
            result = result.rstrip("0").rstrip(".")
        return result or "0"

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes"}
        return bool(value)

    @staticmethod
    def _error_message(status_code: int, body: str) -> str:
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                return f"Binance HTTP {status_code}: {body[:300]}"
            code = payload.get("code", status_code)
            message = payload.get("msg", payload.get("message", body))
            return f"Binance 错误 {code}: {message}"
        except json.JSONDecodeError:
            return f"Binance HTTP {status_code}: {body[:300]}"

    @staticmethod
    def _exchange_error_code(body: str) -> int | None:
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or "code" not in payload:
                return None
            return int(payload["code"])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

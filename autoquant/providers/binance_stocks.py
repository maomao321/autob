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
from decimal import Decimal, ROUND_DOWN
from threading import Event
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from autoquant.models import Bar, OrderRequest, OrderResult, Side
from autoquant.providers.base import StatusCallback, TradingProvider


class ProviderError(RuntimeError):
    """A safe, user-facing provider failure."""


class BinanceStocksProvider(TradingProvider):
    name = "binance_stocks"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        live_trading: bool = False,
        rest_base_url: str = "https://api.binance.com",
        websocket_base_url: str = "wss://nbstream.binance.com/equity",
        recv_window: int = 5000,
        request_timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.live_trading = live_trading
        self.rest_base_url = rest_base_url.rstrip("/")
        self.websocket_base_url = websocket_base_url.rstrip("/")
        self.recv_window = recv_window
        self.request_timeout = request_timeout
        self._server_time_offset_ms = 0

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
        streams = f"{symbol}@kline_5m/{symbol}@kline_1d"
        url = f"{self.websocket_base_url}/stream?streams={streams}"
        reconnect_delay = 1.0

        while not stop_event.is_set():
            messages: queue.Queue[tuple[str, Any]] = queue.Queue()

            def on_open(_ws: Any) -> None:
                messages.put(("status", "行情 WebSocket 已连接"))

            def on_message(_ws: Any, raw_message: str) -> None:
                try:
                    payload = json.loads(raw_message)
                    messages.put(("bar", self.parse_kline_message(payload)))
                except (TypeError, ValueError, KeyError) as exc:
                    messages.put(("status", f"忽略无法解析的行情消息: {exc}"))

            def on_error(_ws: Any, error: Any) -> None:
                messages.put(("status", f"行情连接错误: {error}"))

            def on_close(
                _ws: Any,
                status_code: int | None,
                close_message: str | None,
            ) -> None:
                detail = close_message or "连接已关闭"
                messages.put(("status", f"行情连接关闭({status_code}): {detail}"))

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

            while socket_thread.is_alive() and not stop_event.is_set():
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
        client_order_id = f"aq{uuid.uuid4().hex}"
        params: dict[str, Any] = {
            "symbol": order.symbol.upper(),
            "side": order.side.value,
            "orderType": "MARKET",
            "clientOrderId": client_order_id,
        }
        if order.side is Side.BUY:
            params["notional"] = self._decimal_string(
                order.buy_notional.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            )
        else:
            params["quantity"] = self._decimal_string(order.sell_quantity)

        payload = self._signed_request(
            "POST", "/sapi/v1/equity/order/place", params
        )
        accepted = payload.get("status") == "S"
        order_id = str(payload.get("orderId", ""))
        return OrderResult(
            accepted=accepted,
            order_id=order_id,
            message=("实盘订单已接受" if accepted else "实盘订单被拒绝"),
            paper=False,
            raw=payload,
        )

    def check_symbol(self, symbol: str) -> dict:
        if not self.api_key:
            if self.live_trading:
                raise ProviderError("实盘模式必须填写 API Key")
            return {
                "symbol": symbol.upper(),
                "tradability": "UNKNOWN",
                "validation": "模拟模式未提供 API Key，跳过交易所代码校验",
            }
        if self.live_trading:
            self.sync_server_time()
        payload = self._request_json(
            "GET",
            "/sapi/v1/equity/market/exchangeInfo",
            {"symbol": symbol.upper()},
            signed=False,
        )
        if not isinstance(payload, dict):
            raise ProviderError("Binance 股票信息返回结构不符合预期")
        symbols = payload.get("symbols", [])
        if not symbols:
            raise ProviderError(f"Binance Stocks 不支持股票代码 {symbol.upper()}")
        info = dict(symbols[0])
        if self.live_trading:
            tokenized_assets = self._request_json(
                "GET",
                "/sapi/v1/equity/market/tokenized-assets",
                {},
                signed=False,
            )
            if not isinstance(tokenized_assets, list):
                raise ProviderError("Binance 代币化股票列表返回结构不符合预期")
            supported = any(
                str(asset.get("underlyingEquitySymbol", "")).upper()
                == symbol.upper()
                and bool(asset.get("multiplierValid", False))
                for asset in tokenized_assets
                if isinstance(asset, dict)
            )
            if not supported:
                raise ProviderError(f"{symbol.upper()} 当前未启用 Binance 股票交易")
            info["tokenizationEnabled"] = True
        return info

    def sync_server_time(self) -> int:
        payload = self._request_json("GET", "/api/v3/time", {}, signed=False)
        if not isinstance(payload, dict):
            raise ProviderError("Binance 时间接口返回结构不符合预期")
        server_time = int(payload["serverTime"])
        self._server_time_offset_ms = server_time - int(time.time() * 1000)
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
            close_time=int(required("T", "closeTime", "endTime")),
            open=Decimal(str(required("o", "open"))),
            high=Decimal(str(required("h", "high"))),
            low=Decimal(str(required("l", "low"))),
            close=Decimal(str(required("c", "close"))),
            volume=Decimal(str(optional("0", "v", "volume"))),
            closed=cls._as_bool(optional(False, "x", "closed", "isClosed")),
            event_time=int(data.get("E", data.get("eventTime", 0))),
        )

    def _signed_request(
        self, method: str, path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_credentials()
        signed_params = dict(params)
        signed_params["recvWindow"] = self.recv_window
        signed_params["timestamp"] = (
            int(time.time() * 1000) + self._server_time_offset_ms
        )
        query = urlencode(signed_params)
        signed_params["signature"] = self._signature(query)
        payload = self._request_json(method, path, signed_params, signed=True)
        if not isinstance(payload, dict):
            raise ProviderError("Binance 交易接口返回结构不符合预期")
        return payload

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
            "User-Agent": "AutoQuant/0.1",
        }
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        request = Request(url, method=method.upper(), headers=headers)
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(self._error_message(exc.code, body)) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"Binance 请求失败: {exc}") from exc
        if not body.strip():
            return {}
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError("Binance 返回了无效 JSON") from exc
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
            code = payload.get("code", status_code)
            message = payload.get("msg", payload.get("message", body))
            return f"Binance 错误 {code}: {message}"
        except json.JSONDecodeError:
            return f"Binance HTTP {status_code}: {body[:300]}"

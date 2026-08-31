from __future__ import annotations

import json
import queue
import random
import ssl
import threading
import time
import uuid
from collections.abc import Iterator
from decimal import Decimal, ROUND_DOWN
from threading import Event
from typing import Any

from autoquant_shared.models import Bar, OrderRequest, OrderResult, Side
from autoquant_backend.providers.binance_stocks import (
    BinanceStocksProvider,
    OrderRejectedError,
    OrderStatusUnknownError,
    OrderValidationError,
    ProviderError,
    ProviderHTTPError,
    ProviderTransportError,
)


PERPETUAL_CONTRACT_TYPES = {"PERPETUAL", "TRADIFI_PERPETUAL"}


class _SharedFuturesUserStream:
    """Share one account-wide Futures user stream across symbol runners."""

    def __init__(self, provider: "BinanceFuturesProvider") -> None:
        self.provider = provider
        self._lock = threading.RLock()
        self._subscribers: dict[str, queue.Queue[tuple[str, Any]]] = {}
        self._socket: Any = None
        self._thread: threading.Thread | None = None

    def subscribe(self) -> tuple[str, queue.Queue[tuple[str, Any]]]:
        token = uuid.uuid4().hex
        messages: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=512)
        with self._lock:
            self._subscribers[token] = messages
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="binance-futures-user-stream",
                    daemon=True,
                )
                self._thread.start()
        return token, messages

    def unsubscribe(self, token: str) -> None:
        with self._lock:
            self._subscribers.pop(token, None)
            empty = not self._subscribers
            socket = self._socket
        if empty and socket is not None:
            socket.close()

    def _snapshot(self) -> list[queue.Queue[tuple[str, Any]]]:
        with self._lock:
            return list(self._subscribers.values())

    @staticmethod
    def _put(
        messages: queue.Queue[tuple[str, Any]], item: tuple[str, Any]
    ) -> None:
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

    def _broadcast(self, kind: str, value: Any) -> None:
        for messages in self._snapshot():
            self._put(messages, (kind, value))

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            self._broadcast(
                "error",
                "缺少 websocket-client，请先执行: python -m pip install -e .",
            )
            return

        reconnect_delay = 1.0
        while self._snapshot():
            listen_key = ""
            try:
                payload = self.provider._request_json(
                    "POST", "/fapi/v1/listenKey", {}, signed=False
                )
                if isinstance(payload, dict):
                    listen_key = str(payload.get("listenKey", "")).strip()
                if not listen_key:
                    raise ProviderError("Futures 用户数据流未返回 listenKey")
            except Exception as exc:
                self._broadcast("status", f"订单事件流连接失败: {exc}")
                if not self._wait_before_reconnect(reconnect_delay):
                    return
                reconnect_delay = min(reconnect_delay * 2, 30.0)
                continue

            keepalive_stop = threading.Event()

            def on_open(_ws: Any) -> None:
                nonlocal reconnect_delay
                reconnect_delay = 1.0
                self._broadcast("status", "订单事件流已连接")

            def on_message(_ws: Any, raw_message: str) -> None:
                try:
                    payload = json.loads(raw_message)
                except (TypeError, json.JSONDecodeError):
                    return
                if isinstance(payload, dict):
                    self._broadcast("event", payload)

            def on_error(_ws: Any, error: Any) -> None:
                self._broadcast("status", f"订单事件流错误: {error}")

            ws = websocket.WebSocketApp(
                self.provider._user_stream_url(listen_key),
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
            )
            with self._lock:
                self._socket = ws
            keepalive_thread = threading.Thread(
                target=self._keepalive,
                args=(listen_key, keepalive_stop, ws),
                name="binance-futures-user-stream-keepalive",
                daemon=True,
            )
            keepalive_thread.start()
            ws.run_forever(
                ping_interval=20,
                ping_timeout=10,
                sslopt={"cert_reqs": ssl.CERT_REQUIRED},
            )
            keepalive_stop.set()
            keepalive_thread.join(timeout=1)
            with self._lock:
                if self._socket is ws:
                    self._socket = None
            if not self._snapshot():
                try:
                    self.provider._request_json(
                        "DELETE", "/fapi/v1/listenKey", {}, signed=False
                    )
                except Exception:
                    pass
                return
            if not self._wait_before_reconnect(reconnect_delay):
                return
            reconnect_delay = min(reconnect_delay * 2, 30.0)

    def _keepalive(
        self, listen_key: str, stop_event: Event, socket: Any
    ) -> None:
        while not stop_event.wait(30 * 60):
            try:
                self.provider._request_json(
                    "PUT", "/fapi/v1/listenKey", {}, signed=False
                )
            except Exception as exc:
                self._broadcast("status", f"订单事件流续期失败: {exc}")
                socket.close()
                return

    def _wait_before_reconnect(self, base_delay: float) -> bool:
        delay = min(30.0, base_delay * random.uniform(0.8, 1.2))
        self._broadcast("status", f"订单事件流将在 {delay:.1f} 秒后重连")
        deadline = time.monotonic() + delay
        while self._snapshot() and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return bool(self._snapshot())


class BinanceFuturesProvider(BinanceStocksProvider):
    """Binance USDⓈ-M Futures provider using one-way net positions."""

    name = "binance_futures"
    supports_short = True
    quote_asset = "USDT"
    _futures_exchange_info_cache: tuple[float, dict[str, Any]] | None = None
    _user_streams_lock = threading.RLock()
    _user_streams: dict[tuple[str, str, str], _SharedFuturesUserStream] = {}

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        live_trading: bool = False,
        leverage: int = 1,
        rest_base_url: str = "https://fapi.binance.com",
        websocket_base_url: str = "wss://fstream.binance.com",
        recv_window: int = 5000,
        request_timeout: float = 10.0,
        include_daily_stream: bool = True,
    ) -> None:
        super().__init__(
            api_key=api_key,
            api_secret=api_secret,
            live_trading=live_trading,
            rest_base_url=rest_base_url,
            websocket_base_url=websocket_base_url,
            recv_window=recv_window,
            request_timeout=request_timeout,
            include_daily_stream=include_daily_stream,
        )
        self.leverage = int(leverage)
        if not 1 <= self.leverage <= 125:
            raise ValueError("Binance Futures 杠杆倍数必须在 1 到 125 之间")
        self._prepared_symbols: set[str] = set()

    def _stream_url(self, symbol: str) -> str:
        return self._combined_stream_url([symbol])

    def _combined_stream_url(self, symbols: list[str]) -> str:
        streams = "/".join(
            stream
            for symbol in symbols
            for stream in (
                [f"{symbol.lower()}@kline_5m", f"{symbol.lower()}@kline_1d"]
                if self.include_daily_stream
                else [f"{symbol.lower()}@kline_5m"]
            )
        )
        return f"{self.websocket_base_url}/market/stream?streams={streams}"

    def stream_order_updates(
        self,
        stop_event: Event,
        status_callback: Any = None,
    ) -> Iterator[dict[str, Any]]:
        self._require_credentials()
        key = (self.api_key, self.rest_base_url, self.websocket_base_url)
        with self._user_streams_lock:
            stream = self._user_streams.get(key)
            if stream is None:
                stream = _SharedFuturesUserStream(self)
                self._user_streams[key] = stream
        token, messages = stream.subscribe()
        try:
            while not stop_event.is_set():
                try:
                    kind, value = messages.get(timeout=0.5)
                except queue.Empty:
                    continue
                if kind == "event" and isinstance(value, dict):
                    yield value
                elif kind == "error":
                    raise ProviderError(str(value))
                elif status_callback:
                    status_callback(str(value))
        finally:
            stream.unsubscribe(token)

    def _user_stream_url(self, listen_key: str) -> str:
        return f"{self.websocket_base_url}/private/ws/{listen_key}"

    def check_symbol(self, symbol: str) -> dict:
        symbol = symbol.strip().upper()
        cached = self._symbol_info.get(symbol)
        cached_at = self._symbol_info_cached_at.get(symbol, 0.0)
        if cached is not None and time.monotonic() - cached_at < 60:
            return dict(cached)

        payload = self._cached_exchange_info()
        symbols = payload.get("symbols", [])
        info = next(
            (
                dict(item)
                for item in symbols
                if isinstance(item, dict)
                and str(item.get("symbol", "")).upper() == symbol
            ),
            None,
        )
        if info is None:
            raise ProviderError(f"Binance USDⓈ-M Futures 不支持标的 {symbol}")
        if str(info.get("status", "")).upper() != "TRADING":
            raise ProviderError(
                f"{symbol} 当前合约状态为 {info.get('status', 'UNKNOWN')}"
            )
        contract_type = str(info.get("contractType", "")).upper()
        if contract_type not in PERPETUAL_CONTRACT_TYPES:
            raise ProviderError(f"{symbol} 不是 USDⓈ-M 永续合约")
        info["tradability"] = "BUY_SELL"
        contract_label = (
            "TradFi 永续合约"
            if contract_type == "TRADIFI_PERPETUAL"
            else "永续合约"
        )
        info["validation"] = (
            f"USDⓈ-M {contract_label}校验通过；"
            f"实盘首单前设置杠杆 {self.leverage}x"
        )
        self._symbol_info[symbol] = info
        self._symbol_info_cached_at[symbol] = time.monotonic()
        return dict(info)

    def _cached_exchange_info(self) -> dict[str, Any]:
        with self._public_cache_lock:
            cached = self.__class__._futures_exchange_info_cache
            if cached is not None and time.monotonic() - cached[0] < 60:
                return cached[1]
            payload = self._request_json(
                "GET", "/fapi/v1/exchangeInfo", {}, signed=False
            )
            if not isinstance(payload, dict):
                raise ProviderError(
                    "Binance Futures 交易规则返回结构不符合预期"
                )
            self.__class__._futures_exchange_info_cache = (
                time.monotonic(),
                payload,
            )
            return payload

    def get_24h_rankings(self, limit: int = 20) -> dict[str, Any]:
        """Return separate crypto and stock USDT perpetual 24h rankings."""
        if not 1 <= int(limit) <= 100:
            raise ValueError("涨跌榜数量必须在 1 到 100 之间")

        exchange_info = self._cached_exchange_info()
        symbol_markets = {
            str(item.get("symbol", "")).upper(): (
                "stock"
                if str(item.get("contractType", "")).upper()
                == "TRADIFI_PERPETUAL"
                else "crypto"
            )
            for item in exchange_info.get("symbols", [])
            if isinstance(item, dict)
            and str(item.get("status", "")).upper() == "TRADING"
            and str(item.get("contractType", "")).upper()
            in PERPETUAL_CONTRACT_TYPES
            and str(item.get("quoteAsset", "")).upper() == "USDT"
        }
        payload = self._request_json(
            "GET", "/fapi/v1/ticker/24hr", {}, signed=False
        )
        if not isinstance(payload, list):
            raise ProviderError("Binance Futures 24 小时行情返回结构不符合预期")

        rows: list[tuple[Decimal, dict[str, str]]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper()
            market = symbol_markets.get(symbol)
            if market is None:
                continue
            try:
                change = Decimal(str(item.get("priceChangePercent", "")))
                last_price = Decimal(str(item.get("lastPrice", "")))
                quote_volume = Decimal(str(item.get("quoteVolume", "0")))
            except (ArithmeticError, ValueError):
                continue
            if (
                not change.is_finite()
                or not last_price.is_finite()
                or last_price <= 0
                or not quote_volume.is_finite()
                or quote_volume < 0
            ):
                continue
            rows.append(
                (
                    change,
                    {
                        "symbol": symbol,
                        "market": market,
                        "price_change_percent": format(change, "f"),
                        "last_price": format(last_price, "f"),
                        "quote_volume": format(quote_volume, "f"),
                    },
                )
            )

        def ranked(market: str, *, gaining: bool) -> list[dict[str, str]]:
            selected = [
                row
                for change, row in rows
                if row["market"] == market
                and (change > 0 if gaining else change < 0)
            ]
            selected.sort(
                key=lambda row: Decimal(row["price_change_percent"]),
                reverse=gaining,
            )
            return selected[: int(limit)]

        return {
            "stock_gainers": ranked("stock", gaining=True),
            "stock_losers": ranked("stock", gaining=False),
            "crypto_gainers": ranked("crypto", gaining=True),
            "crypto_losers": ranked("crypto", gaining=False),
            "tickers": {row["symbol"]: row for _change, row in rows},
            "updated_at": int(time.time() * 1000),
            "window": "24h",
        }

    def place_order(self, order: OrderRequest) -> OrderResult:
        if not self.live_trading:
            order_id = f"paper-futures-{uuid.uuid4().hex}"
            if order.reduce_only:
                position = "多头" if order.side is Side.SELL else "空头"
                size = f"平{position}数量 {order.sell_quantity}"
            else:
                position = "多头" if order.side is Side.BUY else "空头"
                size = (
                    f"开{position}名义金额 {order.buy_notional} "
                    f"{self.quote_asset}"
                )
            return OrderResult(
                accepted=True,
                order_id=order_id,
                message=f"模拟 Futures {order.side.value} 已记录，{size}，杠杆 {self.leverage}x",
                paper=True,
                raw={"status": "FILLED", "orderId": order_id},
            )

        self._require_credentials()
        symbol = order.symbol.strip().upper()
        try:
            self._prepare_live_symbol(symbol)
        except OrderValidationError:
            raise
        except ProviderError as exc:
            raise OrderValidationError(
                f"Futures 下单准备失败，尚未发送订单：{exc}"
            ) from exc
        try:
            quantity = self._format_futures_quantity(order)
        except OrderValidationError:
            raise
        except (ArithmeticError, ValueError) as exc:
            raise OrderValidationError(f"Futures 订单数量格式无效: {exc}") from exc

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": order.side.value,
            "type": "MARKET",
            "quantity": self._decimal_string(quantity),
            "newClientOrderId": order.client_order_id or f"aq{uuid.uuid4().hex}",
            "newOrderRespType": "ACK",
        }
        if order.reduce_only:
            params["reduceOnly"] = "true"
        try:
            payload = self._signed_request("POST", "/fapi/v1/order", params)
        except ProviderHTTPError as exc:
            execution_unknown = exc.exchange_code in {-1006, -1007}
            if 400 <= exc.status_code < 500 and not execution_unknown:
                raise OrderRejectedError(str(exc)) from exc
            raise OrderStatusUnknownError(str(exc)) from exc
        except ProviderTransportError as exc:
            raise OrderStatusUnknownError(str(exc)) from exc

        order_id = str(payload.get("orderId", ""))
        status = str(payload.get("status", "NEW")).upper()
        accepted = bool(order_id) and status not in {"REJECTED", "EXPIRED"}
        if not order_id:
            raise OrderStatusUnknownError(
                "Binance Futures 已响应订单，但缺少 orderId"
            )
        return OrderResult(
            accepted=accepted,
            order_id=order_id,
            message=(
                f"Futures 实盘订单已接受，杠杆 {self.leverage}x"
                if accepted
                else "Futures 实盘订单被拒绝"
            ),
            paper=False,
            raw=payload,
        )

    def _prepare_live_symbol(self, symbol: str) -> None:
        if symbol in self._prepared_symbols:
            return
        self.check_symbol(symbol)
        self._ensure_server_time()
        position_mode = self._signed_request(
            "GET", "/fapi/v1/positionSide/dual", {}
        )
        if self._as_bool(position_mode.get("dualSidePosition", False)):
            raise OrderValidationError(
                "当前 Futures 账户为双向持仓模式；本程序仅支持单向持仓模式，请先在 Binance 切换"
            )
        leverage_result = self._signed_request(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": self.leverage},
        )
        try:
            applied = int(leverage_result.get("leverage", 0))
        except (TypeError, ValueError) as exc:
            raise ProviderError("Binance Futures 杠杆响应格式无效") from exc
        if applied != self.leverage:
            raise ProviderError(
                f"Binance Futures 返回杠杆 {applied}x，与请求的 {self.leverage}x 不一致"
            )
        self._prepared_symbols.add(symbol)

    def _format_futures_quantity(self, order: OrderRequest) -> Decimal:
        symbol = order.symbol.strip().upper()
        info = self._symbol_info.get(symbol, {})
        if not order.reduce_only:
            self._require_positive_finite(order.buy_notional, "名义金额")
            self._require_positive_finite(order.reference_price, "参考价格")
            quantity = order.buy_notional / order.reference_price
        else:
            quantity = order.sell_quantity
            self._require_positive_finite(quantity, "减仓数量")

        step_size = self._rule(info, "stepSize", "MARKET_LOT_SIZE", "LOT_SIZE")
        if step_size is not None and step_size > 0:
            quantity = (
                (quantity / step_size).to_integral_value(rounding=ROUND_DOWN)
                * step_size
            )
        if quantity <= 0:
            raise OrderValidationError("订单数量低于 Futures 最小步长")
        self._validate_range(
            quantity,
            self._rule(info, "minQty", "MARKET_LOT_SIZE", "LOT_SIZE"),
            self._rule(info, "maxQty", "MARKET_LOT_SIZE", "LOT_SIZE"),
            "订单数量",
        )
        estimated_notional = quantity * order.reference_price
        self._validate_range(
            estimated_notional,
            self._rule(info, "notional", "MIN_NOTIONAL"),
            None,
            "预计名义金额",
        )
        return quantity

    def get_order_detail(self, order_id: str, symbol: str = "") -> dict:
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ProviderError("查询 Futures 订单必须提供标的代码")
        return self._signed_request(
            "GET",
            "/fapi/v1/order",
            {"symbol": normalized_symbol, "orderId": order_id},
        )

    def get_account_total(self, quote_asset: str = "USDT") -> Decimal:
        self._require_credentials()
        self._ensure_server_time()
        normalized_quote = quote_asset.strip().upper()
        payload = self._signed_request_payload(
            "GET", "/fapi/v3/balance", {}
        )
        if not isinstance(payload, list):
            raise ProviderError("Binance Futures 账户余额返回结构不符合预期")
        for item in payload:
            if not isinstance(item, dict):
                continue
            if str(item.get("asset", "")).upper() != normalized_quote:
                continue
            try:
                balance = Decimal(str(item.get("balance", "")))
            except (ArithmeticError, ValueError) as exc:
                raise ProviderError("Binance Futures 账户余额无效") from exc
            if not balance.is_finite() or balance < 0:
                raise ProviderError("Binance Futures 账户余额无效")
            return balance
        return Decimal("0")

    def get_latest_price(self, symbol: str) -> Decimal:
        symbol = symbol.strip().upper()
        cached_price = self._cached_latest_price(symbol)
        if cached_price is not None:
            return cached_price
        payload = self._request_json(
            "GET",
            "/fapi/v1/ticker/bookTicker",
            {"symbol": symbol},
            signed=False,
        )
        if not isinstance(payload, dict):
            raise ProviderError(f"{symbol} 当前没有可用 Futures 报价")
        price = self._quote_midpoint(payload)
        if price is None:
            raise ProviderError(f"{symbol} 当前没有有效 Futures 买卖报价")
        return self._cache_latest_price(symbol, price)

    def get_historical_bars(
        self,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        limit: int,
    ) -> list[Bar]:
        if interval not in {"1d", "5m", "1m"}:
            raise ProviderError(f"Futures 历史 K 线不支持周期 {interval}")
        if limit <= 0 or end_time < start_time:
            return []
        payload = self._request_json(
            "GET",
            "/fapi/v1/klines",
            {
                "symbol": symbol.strip().upper(),
                "interval": interval,
                "startTime": start_time,
                "endTime": end_time,
                "limit": min(limit, 1500),
            },
            signed=False,
        )
        if not isinstance(payload, list):
            raise ProviderError("Binance Futures 历史 K 线返回结构不符合预期")
        bars: list[Bar] = []
        now_ms = int(time.time() * 1000)
        for item in payload:
            if not isinstance(item, list) or len(item) < 7:
                continue
            try:
                bar = Bar(
                    symbol=symbol.strip().upper(),
                    interval=interval,
                    open_time=int(item[0]),
                    close_time=int(item[6]),
                    open=Decimal(str(item[1])),
                    high=Decimal(str(item[2])),
                    low=Decimal(str(item[3])),
                    close=Decimal(str(item[4])),
                    volume=Decimal(str(item[5])),
                    closed=int(item[6]) < now_ms,
                )
            except (ArithmeticError, TypeError, ValueError):
                continue
            if (
                bar.closed
                and start_time <= bar.open_time
                and bar.close_time <= end_time
            ):
                bars.append(bar)
        bars.sort(key=lambda bar: bar.open_time)
        return bars[-limit:]

    def sync_server_time(self) -> int:
        return self._sync_server_time(
            "/fapi/v1/time",
            "Binance Futures 时间接口返回结构不符合预期",
        )

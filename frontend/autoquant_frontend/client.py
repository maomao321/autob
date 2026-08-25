from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from autoquant_shared.config import AppConfig
from autoquant_shared.models import (
    AccountOverview,
    Direction,
    RunState,
    RuntimeSnapshot,
    TradeHistoryItem,
)


class BackendClientError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RemoteRunnerConfig:
    app: AppConfig
    api_key: str = ""
    api_secret: str = ""
    openai_api_key: str = ""
    deepseek_api_key: str = ""
    manual_direction: Direction = Direction.FLAT


class BackendClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("AUTOQUANT_SERVER_URL", "http://127.0.0.1:8765")
        ).rstrip("/")
        parsed_url = urlparse(self.base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise ValueError("AUTOQUANT_SERVER_URL 必须是有效的 HTTP(S) 地址")
        loopback = parsed_url.hostname in {"127.0.0.1", "::1", "localhost"}
        if (
            parsed_url.scheme != "https"
            and not loopback
            and os.environ.get("AUTOQUANT_ALLOW_INSECURE_HTTP", "").strip() != "1"
        ):
            raise ValueError(
                "远程后端必须使用 HTTPS；仅受信任内网调试可设置 "
                "AUTOQUANT_ALLOW_INSECURE_HTTP=1"
            )
        self.api_token = (
            os.environ.get("AUTOQUANT_API_TOKEN", "")
            if api_token is None
            else api_token
        )
        self.timeout = timeout

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "AutoQuant-Frontend/0.5.0",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(
            f"{self.base_url}{path}", data=body, method=method, headers=headers
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw).get("error", raw)
            except json.JSONDecodeError:
                detail = raw
            raise BackendClientError(f"后端 HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BackendClientError(f"无法连接后端 {self.base_url}: {exc}") from exc
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise BackendClientError("后端返回了无效 JSON") from exc

    def load_config(self) -> AppConfig:
        return AppConfig(**self.request("GET", "/api/v1/config"))

    def save_config(self, config: AppConfig) -> AppConfig:
        return AppConfig(**self.request("PUT", "/api/v1/config", asdict(config)))


class RemoteConfigStore:
    def __init__(self, client: BackendClient) -> None:
        self.client = client
        self.path = f"{client.base_url}/api/v1/config"

    def load(self) -> AppConfig:
        return self.client.load_config()

    def save(self, config: AppConfig) -> None:
        self.client.save_config(config)


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _snapshot(payload: dict[str, Any]) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        symbol=str(payload["symbol"]),
        state=RunState(str(payload["state"])),
        direction=Direction(str(payload["direction"])),
        last_price=_optional_decimal(payload.get("last_price")),
        ma_value=_optional_decimal(payload.get("ma_value")),
        warmup_bars=int(payload.get("warmup_bars", 0)),
        warmup_required=int(payload.get("warmup_required", 0)),
        trades_today=int(payload.get("trades_today", 0)),
        position_quantity=Decimal(str(payload.get("position_quantity", "0"))),
        average_entry_price=Decimal(str(payload.get("average_entry_price", "0"))),
        pending_orders=int(payload.get("pending_orders", 0)),
        daily_buy_notional=Decimal(str(payload.get("daily_buy_notional", "0"))),
        realized_pnl=Decimal(str(payload.get("realized_pnl", "0"))),
        unrealized_pnl=_optional_decimal(payload.get("unrealized_pnl")),
        profit=_optional_decimal(payload.get("profit")),
        message=str(payload.get("message", "")),
        updated_at=int(payload.get("updated_at", 0)),
    )


def _overview(payload: dict[str, Any]) -> AccountOverview:
    return AccountOverview(
        total_balance=_optional_decimal(payload.get("total_balance")),
        realized_pnl=Decimal(str(payload.get("realized_pnl", "0"))),
        unrealized_pnl=_optional_decimal(payload.get("unrealized_pnl")),
        currency=str(payload.get("currency", "USDC")),
        missing_price_symbols=tuple(payload.get("missing_price_symbols", [])),
        message=str(payload.get("message", "")),
        updated_at=int(payload.get("updated_at", 0)),
    )


def _trade_history_item(payload: dict[str, Any]) -> TradeHistoryItem:
    return TradeHistoryItem(
        executed_at=int(payload.get("executed_at", 0)),
        symbol=str(payload.get("symbol", "")),
        action=str(payload.get("action", "")),
        opening_direction=str(payload.get("opening_direction", "")),
        price=Decimal(str(payload.get("price", "0"))),
        quantity=Decimal(str(payload.get("quantity", "0"))),
        amount=Decimal(str(payload.get("amount", "0"))),
        fee=Decimal(str(payload.get("fee", "0"))),
        profit=Decimal(str(payload.get("profit", "0"))),
        paper=bool(payload.get("paper", True)),
    )


class RemoteTradingController:
    def __init__(
        self,
        client: BackendClient,
        snapshot_callback: Callable[[RuntimeSnapshot], None],
        log_callback: Callable[[str, str, str], None],
        poll_interval: float = 1.0,
    ) -> None:
        self.client = client
        self.snapshot_callback = snapshot_callback
        self.log_callback = log_callback
        self.poll_interval = max(0.2, poll_interval)
        self._last_log = 0
        self._closed = threading.Event()
        self._poll_thread = threading.Thread(
            target=self._poll, name="autoquant-backend-poll", daemon=True
        )
        self._poll_thread.start()

    def _poll(self) -> None:
        reported_error = ""
        while not self._closed.is_set():
            try:
                query = urlencode({"after_log": self._last_log})
                payload = self.client.request("GET", f"/api/v1/status?{query}")
                for item in payload.get("snapshots", []):
                    self.snapshot_callback(_snapshot(item))
                for item in payload.get("logs", []):
                    self.log_callback(
                        str(item.get("level", "INFO")),
                        str(item.get("symbol", "SYSTEM")),
                        str(item.get("message", "")),
                    )
                self._last_log = int(payload.get("last_log_sequence", self._last_log))
                reported_error = ""
            except Exception as exc:
                message = str(exc)
                if message != reported_error:
                    self.log_callback("ERROR", "BACKEND", message)
                    reported_error = message
            self._closed.wait(self.poll_interval)

    def _sync_config(self, config: RemoteRunnerConfig) -> None:
        self.client.save_config(config.app)

    def start(self, symbol: str, config: RemoteRunnerConfig) -> None:
        self._sync_config(config)
        self.client.request(
            "POST",
            f"/api/v1/runners/{quote(symbol.upper())}/start",
            {"direction": config.manual_direction.value},
        )

    def stop(self, symbol: str, *, close_position: bool = False) -> None:
        self.client.request(
            "POST",
            f"/api/v1/runners/{quote(symbol.upper())}/stop",
            {"close_position": close_position},
        )

    def stop_all(self, *, close_position: bool = False) -> None:
        for symbol, _mode, _quantity in self.stop_targets():
            self.stop(symbol, close_position=close_position)

    def stop_targets(
        self, symbols: list[str] | None = None
    ) -> list[tuple[str, str, Decimal]]:
        payload = self.client.request(
            "POST", "/api/v1/stop-targets", {"symbols": symbols}
        )
        return [
            (str(item["symbol"]), str(item["mode"]), Decimal(str(item["quantity"])))
            for item in payload.get("targets", [])
        ]

    def unknown_live_orders(self, symbol: str) -> int:
        payload = self.client.request(
            "GET", f"/api/v1/runners/{quote(symbol.upper())}/unknown-orders"
        )
        return int(payload.get("count", 0))

    def resolve_unknown_live_orders(self, symbol: str) -> int:
        payload = self.client.request(
            "POST",
            f"/api/v1/runners/{quote(symbol.upper())}/resolve-unknown",
            {},
        )
        return int(payload.get("resolved", 0))

    def check_connection(self, symbol: str, config: RemoteRunnerConfig) -> str:
        self._sync_config(config)
        payload = self.client.request(
            "POST", "/api/v1/connection/check", {"symbol": symbol}
        )
        return str(payload.get("message", "连接成功"))

    def account_overview(
        self, config: RemoteRunnerConfig, market_prices: dict[str, Decimal]
    ) -> AccountOverview:
        payload = self.client.request(
            "POST",
            "/api/v1/account/overview",
            {
                "market_prices": {
                    key: format(value, "f") for key, value in market_prices.items()
                }
            },
        )
        return _overview(payload)

    def trade_history(
        self,
        *,
        symbol: str = "",
        action: str = "ALL",
        mode: str = "ALL",
        limit: int = 500,
    ) -> list[TradeHistoryItem]:
        query = urlencode(
            {
                "symbol": symbol.strip().upper(),
                "action": action.strip().upper(),
                "mode": mode.strip().upper(),
                "limit": min(max(int(limit), 1), 1000),
            }
        )
        payload = self.client.request("GET", f"/api/v1/trades?{query}")
        return [
            _trade_history_item(item)
            for item in payload.get("items", [])
            if isinstance(item, dict)
        ]

    def wait_for_all(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if not self.stop_targets():
                return True
            time.sleep(0.2)
        return not self.stop_targets()

    def join_all(self, timeout_per_runner: float = 0.0) -> None:
        return

    def close(self) -> None:
        self._closed.set()
        self._poll_thread.join(timeout=2)


__all__ = [
    "BackendClient",
    "BackendClientError",
    "RemoteConfigStore",
    "RemoteRunnerConfig",
    "RemoteTradingController",
]

from __future__ import annotations

import argparse
import hmac
import json
import os
import signal
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from autoquant_backend.runtime import BackendRuntime


DEFAULT_MAX_CONNECTIONS = 32
DEFAULT_CONNECTION_TIMEOUT = 15.0
MAX_ARCHIVE_UPLOAD_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BinaryPayload:
    body: bytes
    content_type: str
    filename: str


class AutoQuantHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self,
        server_address: tuple[str, int],
        runtime: BackendRuntime,
        api_token: str,
        *,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT,
    ) -> None:
        if max_connections <= 0:
            raise ValueError("HTTP 最大并发连接数必须大于 0")
        if connection_timeout <= 0:
            raise ValueError("HTTP 连接超时必须大于 0")
        self.max_connections = max_connections
        self.connection_timeout = connection_timeout
        self._connection_slots = threading.BoundedSemaphore(max_connections)
        super().__init__(server_address, AutoQuantRequestHandler)
        self.runtime = runtime
        self.api_token = api_token

    def get_request(self) -> tuple[Any, Any]:
        request, client_address = super().get_request()
        request.settimeout(self.connection_timeout)
        return request, client_address

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class AutoQuantRequestHandler(BaseHTTPRequestHandler):
    server: AutoQuantHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("请求体过大")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是 JSON 对象") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _binary_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0:
            raise ValueError("导入文件为空")
        if length > MAX_ARCHIVE_UPLOAD_BYTES:
            raise ValueError("导入文件超过 128 MB 限制")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("导入文件上传不完整")
        return body

    def _authorized(self) -> bool:
        token = self.server.api_token
        if not token:
            return self.client_address[0] in {"127.0.0.1", "::1"}
        supplied = self.headers.get("Authorization", "")
        prefix = "Bearer "
        return supplied.startswith(prefix) and hmac.compare_digest(
            supplied[len(prefix) :], token
        )

    def _send(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_binary(self, status: HTTPStatus, payload: BinaryPayload) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", payload.content_type)
        self.send_header("Content-Length", str(len(payload.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''" + quote(payload.filename, safe=""),
        )
        self.end_headers()
        self.wfile.write(payload.body)

    def _dispatch(self) -> tuple[HTTPStatus, Any]:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health" and self.command == "GET":
            return HTTPStatus.OK, {"status": "ok", "service": "autoquant"}
        if not self._authorized():
            return HTTPStatus.UNAUTHORIZED, {"error": "未授权"}

        runtime = self.server.runtime
        if path == "/api/v1/config":
            if self.command == "GET":
                return HTTPStatus.OK, runtime.public_config()
            if self.command == "PUT":
                return HTTPStatus.OK, runtime.save_config(self._json_body())
        if path == "/api/v1/status" and self.command == "GET":
            query = parse_qs(parsed.query)
            after = int(query.get("after_log", ["0"])[0])
            return HTTPStatus.OK, runtime.status(after)
        if path == "/api/v1/stop-targets" and self.command == "POST":
            payload = self._json_body()
            symbols = payload.get("symbols")
            if symbols is not None and not isinstance(symbols, list):
                raise ValueError("symbols 必须是列表")
            return HTTPStatus.OK, {"targets": runtime.stop_targets(symbols)}
        if path == "/api/v1/connection/check" and self.command == "POST":
            payload = self._json_body()
            return HTTPStatus.OK, runtime.check_connection(str(payload["symbol"]))
        if path == "/api/v1/account/overview" and self.command == "POST":
            prices = self._json_body().get("market_prices", {})
            if not isinstance(prices, dict):
                raise ValueError("market_prices 必须是对象")
            return HTTPStatus.OK, runtime.account_overview(prices)
        if path == "/api/v1/trades" and self.command == "GET":
            query = parse_qs(parsed.query)
            mode = query.get("mode", ["ALL"])[0].strip().upper()
            if mode not in {"ALL", "PAPER", "REAL"}:
                raise ValueError("交易模式必须是 ALL、PAPER 或 REAL")
            return HTTPStatus.OK, runtime.trade_history(
                symbol=query.get("symbol", [""])[0],
                action=query.get("action", ["ALL"])[0],
                paper=None if mode == "ALL" else mode == "PAPER",
                limit=int(query.get("limit", ["500"])[0]),
            )
        if path == "/api/v1/ai-decisions" and self.command == "GET":
            query = parse_qs(parsed.query)
            return HTTPStatus.OK, runtime.ai_decision_history(
                symbol=query.get("symbol", [""])[0],
                stage=query.get("stage", ["ALL"])[0],
                limit=int(query.get("limit", ["100"])[0]),
            )
        if path == "/api/v1/backtest/downloads":
            if self.command == "GET":
                query = parse_qs(parsed.query)
                return HTTPStatus.OK, runtime.historical_downloads(
                    limit=int(query.get("limit", ["50"])[0])
                )
            if self.command == "POST":
                payload = self._json_body()
                return HTTPStatus.ACCEPTED, runtime.start_historical_download(
                    str(payload.get("symbol", ""))
                )
        if path == "/api/v1/backtest/bars/export" and self.command == "GET":
            query = parse_qs(parsed.query)
            body, filename = runtime.export_historical_bars(
                query.get("symbol", [""])[0],
                query.get("provider", [""])[0],
            )
            return HTTPStatus.OK, BinaryPayload(
                body=body,
                content_type="application/zip",
                filename=filename,
            )
        if path == "/api/v1/backtest/bars/import" and self.command == "POST":
            query = parse_qs(parsed.query)
            return HTTPStatus.OK, runtime.import_historical_bars(
                self._binary_body(),
                expected_symbol=query.get("symbol", [""])[0],
            )
        if path == "/api/v1/backtest/bars" and self.command == "DELETE":
            query = parse_qs(parsed.query)
            return HTTPStatus.OK, runtime.delete_historical_bars(
                query.get("symbol", [""])[0],
                query.get("provider", [""])[0],
            )
        if path == "/api/v1/backtest/runs":
            if self.command == "GET":
                query = parse_qs(parsed.query)
                return HTTPStatus.OK, runtime.backtest_runs(
                    limit=int(query.get("limit", ["100"])[0])
                )
            if self.command == "POST":
                return HTTPStatus.ACCEPTED, runtime.start_backtest(
                    self._json_body()
                )
        if path == "/api/v1/backtest/trades" and self.command == "GET":
            query = parse_qs(parsed.query)
            return HTTPStatus.OK, runtime.backtest_trade_details(
                query.get("run_id", [""])[0],
                limit=int(query.get("limit", ["50000"])[0]),
            )

        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) >= 5 and parts[:3] == ["api", "v1", "runners"]:
            symbol = parts[3]
            action = parts[4]
            if action == "start" and self.command == "POST":
                payload = self._json_body()
                runtime.start(
                    symbol,
                    str(payload.get("direction", "FLAT")),
                    openai_api_key=str(payload.get("openai_api_key", "")),
                    deepseek_api_key=str(payload.get("deepseek_api_key", "")),
                )
                return HTTPStatus.ACCEPTED, {"accepted": True}
            if action == "stop" and self.command == "POST":
                payload = self._json_body()
                runtime.stop(symbol, close_position=bool(payload.get("close_position")))
                return HTTPStatus.ACCEPTED, {"accepted": True}
            if action == "unknown-orders" and self.command == "GET":
                return HTTPStatus.OK, {"count": runtime.unknown_live_orders(symbol)}
            if action == "resolve-unknown" and self.command == "POST":
                return HTTPStatus.OK, {
                    "resolved": runtime.resolve_unknown_live_orders(symbol)
                }
        return HTTPStatus.NOT_FOUND, {"error": "接口不存在"}

    def _handle(self) -> None:
        try:
            status, payload = self._dispatch()
        except (KeyError, TypeError, ValueError) as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        except RuntimeError as exc:
            status, payload = HTTPStatus.CONFLICT, {"error": str(exc)}
        except Exception as exc:
            status, payload = HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "服务器处理失败",
                "detail": str(exc),
            }
        if isinstance(payload, BinaryPayload):
            self._send_binary(status, payload)
        else:
            self._send(status, payload)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


def create_server(
    host: str,
    port: int,
    *,
    runtime: BackendRuntime | None = None,
    api_token: str | None = None,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
    connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT,
) -> AutoQuantHTTPServer:
    token = os.environ.get("AUTOQUANT_API_TOKEN", "") if api_token is None else api_token
    if host not in {"127.0.0.1", "::1", "localhost"} and not token:
        raise ValueError("监听非本机地址时必须设置 AUTOQUANT_API_TOKEN")
    return AutoQuantHTTPServer(
        (host, port),
        runtime or BackendRuntime(),
        token,
        max_connections=max_connections,
        connection_timeout=connection_timeout,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoQuant 后端服务")
    parser.add_argument(
        "--host", default=os.environ.get("AUTOQUANT_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("AUTOQUANT_PORT", "8765"))
    )
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    restored = server.runtime.restore_desired_runners()
    if restored:
        print("已恢复运行: " + ", ".join(restored))

    stopping = threading.Event()

    def request_shutdown(_signum: int, _frame: object) -> None:
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    print(f"AutoQuant backend listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        server.runtime.shutdown()


if __name__ == "__main__":
    main()

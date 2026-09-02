from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from autoquant_backend.ai_decision.constants import (
    MAX_HTTP_RESPONSE_BYTES,
    PUBLIC_CACHE_MAX_ENTRIES,
    PUBLIC_CACHE_TTL_SECONDS,
)
from autoquant_backend.ai_decision.models import DecisionError

_PUBLIC_CACHE_LOCK = threading.Lock()
_PUBLIC_CACHE: dict[str, tuple[float, bytes]] = {}
_PUBLIC_INFLIGHT: dict[str, threading.Event] = {}


def _post_json(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    if not api_key:
        raise DecisionError("缺少大模型 API Key")
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AutoQuant/0.5.0",
        },
        method="POST",
    )
    content = _read_request(request, timeout_seconds)
    try:
        result = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DecisionError("大模型 API 返回了无法解析的响应") from exc
    if not isinstance(result, dict):
        raise DecisionError("大模型 API 响应格式错误")
    return result


def _get_bytes(url: str, timeout_seconds: int) -> bytes:
    now = time.monotonic()
    with _PUBLIC_CACHE_LOCK:
        cached = _PUBLIC_CACHE.get(url)
        if cached is not None and now - cached[0] <= PUBLIC_CACHE_TTL_SECONDS:
            return cached[1]
        pending = _PUBLIC_INFLIGHT.get(url)
        if pending is None:
            pending = threading.Event()
            _PUBLIC_INFLIGHT[url] = pending
            owns_request = True
        else:
            owns_request = False
    if not owns_request:
        if not pending.wait(timeout_seconds + 1):
            raise DecisionError("等待共享市场数据超时")
        with _PUBLIC_CACHE_LOCK:
            cached = _PUBLIC_CACHE.get(url)
            if cached is not None and time.monotonic() - cached[0] <= (
                PUBLIC_CACHE_TTL_SECONDS
            ):
                return cached[1]
        raise DecisionError("共享市场数据请求失败")

    request = Request(
        url,
        headers={
            "Accept": (
                "application/json, application/rss+xml, application/xml, "
                "text/xml, text/csv"
            ),
            "User-Agent": "AutoQuant/0.5.0",
        },
        method="GET",
    )
    try:
        content = _read_request(request, timeout_seconds)
    except Exception:
        with _PUBLIC_CACHE_LOCK:
            event = _PUBLIC_INFLIGHT.pop(url, None)
            if event is not None:
                event.set()
        raise
    with _PUBLIC_CACHE_LOCK:
        _PUBLIC_CACHE[url] = (time.monotonic(), content)
        if len(_PUBLIC_CACHE) > PUBLIC_CACHE_MAX_ENTRIES:
            oldest_url = min(
                _PUBLIC_CACHE, key=lambda key: _PUBLIC_CACHE[key][0]
            )
            _PUBLIC_CACHE.pop(oldest_url, None)
        event = _PUBLIC_INFLIGHT.pop(url, None)
        if event is not None:
            event.set()
    return content


def _read_request(request: Request, timeout_seconds: int) -> bytes:
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            content = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise DecisionError(f"HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DecisionError("网络请求失败或超时") from exc
    if len(content) > MAX_HTTP_RESPONSE_BYTES:
        raise DecisionError("远程响应超过大小限制")
    return content



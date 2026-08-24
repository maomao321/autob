from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_REST_URL = "https://api.binance.com"
DEFAULT_WS_URL = "wss://nbstream.binance.com/equity"
ALLOWED_REST_HOSTS = {
    "api.binance.com",
    "api-gcp.binance.com",
    "api1.binance.com",
    "api2.binance.com",
    "api3.binance.com",
    "api4.binance.com",
}
ALLOWED_WS_HOSTS = {"nbstream.binance.com"}
SYMBOL_PATTERN = re.compile(r"[A-Z][A-Z0-9.-]{0,19}", re.ASCII)
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", re.ASCII)
MAX_SYMBOLS = 20
MANUAL_DIRECTION_VALUES = {"AUTO", "LONG", "SHORT", "FLAT"}


@dataclass(slots=True)
class AppConfig:
    symbols: list[str] = field(default_factory=lambda: ["AAPL"])
    manual_directions: dict[str, str] = field(default_factory=dict)
    provider: str = "binance_stocks"
    leverage: int = 1
    api_key: str = ""
    api_secret: str = ""
    strategy: str = "five_minute_breakout"
    trading_mode: str = "PAPER"
    ma_period: int = 5
    buy_notional: str = "100.00"
    sell_quantity: str = "1"
    max_trades_per_day: int = 1
    max_order_notional: str = "100.00"
    max_daily_buy_notional: str = "300.00"
    stop_loss_percent: str = "2.0"
    take_profit_percent: str = "4.0"
    max_signal_age_seconds: int = 30
    ai_provider: str = "DISABLED"
    openai_model: str = "gpt-5.6"
    deepseek_model: str = "deepseek-v4-flash"
    ai_min_confidence: str = "0.70"
    ai_history_days: int = 30
    ai_news_days: int = 7
    ai_news_limit: int = 8
    ai_timeout_seconds: int = 20
    rest_base_url: str = DEFAULT_REST_URL
    websocket_base_url: str = DEFAULT_WS_URL
    recv_window: int = 5000

    def validate(self) -> None:
        self.api_key = str(self.api_key).strip()
        self.api_secret = str(self.api_secret).strip()
        if not isinstance(self.symbols, list):
            raise ValueError("symbols 必须是标的代码列表")
        self.symbols = normalize_symbols(self.symbols)
        if len(self.symbols) > MAX_SYMBOLS:
            raise ValueError(f"标的数量不能超过 {MAX_SYMBOLS} 个")
        if not isinstance(self.manual_directions, dict):
            raise ValueError("manual_directions 必须是股票与手动方向的映射")
        normalized_manual_directions: dict[str, str] = {}
        for raw_symbol, raw_direction in self.manual_directions.items():
            if not isinstance(raw_symbol, str):
                raise ValueError("手动方向的标的代码必须是字符串")
            symbols = normalize_symbols([raw_symbol])
            if not symbols:
                raise ValueError("手动方向的标的代码不能为空")
            direction = str(raw_direction).strip().upper()
            if direction not in MANUAL_DIRECTION_VALUES:
                raise ValueError(
                    f"{symbols[0]} 的手动方向必须是 AUTO、LONG、SHORT 或 FLAT"
                )
            if symbols[0] in self.symbols:
                # AUTO belonged to the previous fallback-only mode. Manual-only
                # mode migrates it to the safest no-entry direction.
                normalized_manual_directions[symbols[0]] = (
                    "FLAT" if direction == "AUTO" else direction
                )
        self.manual_directions = normalized_manual_directions
        self.provider = str(self.provider).strip().lower()
        if self.provider not in {"binance_stocks", "binance_futures"}:
            raise ValueError(f"暂不支持 API 供应商: {self.provider}")
        if self.strategy != "five_minute_breakout":
            raise ValueError(f"暂不支持策略: {self.strategy}")
        self.trading_mode = self.trading_mode.upper()
        if self.trading_mode not in {"PAPER", "REAL"}:
            raise ValueError("交易模式必须是 PAPER 或 REAL")
        self.ai_provider = str(self.ai_provider).upper()
        if self.ai_provider not in {
            "DISABLED",
            "CHATGPT",
            "DEEPSEEK",
            "DUAL",
        }:
            raise ValueError(
                "大模型模式必须是 DISABLED、CHATGPT、DEEPSEEK 或 DUAL"
            )
        self.openai_model = str(self.openai_model).strip()
        self.deepseek_model = str(self.deepseek_model).strip()
        if MODEL_PATTERN.fullmatch(self.openai_model) is None:
            raise ValueError("OpenAI 模型名称格式不正确")
        if MODEL_PATTERN.fullmatch(self.deepseek_model) is None:
            raise ValueError("DeepSeek 模型名称格式不正确")
        try:
            self.ma_period = int(self.ma_period)
            self.leverage = int(self.leverage)
            self.max_trades_per_day = int(self.max_trades_per_day)
            self.recv_window = int(self.recv_window)
            self.max_signal_age_seconds = int(self.max_signal_age_seconds)
            self.ai_history_days = int(self.ai_history_days)
            self.ai_news_days = int(self.ai_news_days)
            self.ai_news_limit = int(self.ai_news_limit)
            self.ai_timeout_seconds = int(self.ai_timeout_seconds)
        except (TypeError, ValueError):
            raise ValueError(
                "MA、杠杆倍数、每日交易次数、信号有效期、AI 数据周期、超时和 "
                "recvWindow 必须是整数"
            ) from None
        if not 1 <= self.leverage <= 125:
            raise ValueError("杠杆倍数必须在 1 到 125 之间")
        if not 2 <= self.ma_period <= 200:
            raise ValueError("MA 周期必须在 2 到 200 之间")
        if not 1 <= self.max_trades_per_day <= 100:
            raise ValueError("每日最多交易次数必须在 1 到 100 之间")
        if not 1 <= self.recv_window <= 5000:
            raise ValueError("为避免过期订单，recvWindow 必须在 1 到 5000 毫秒之间")
        if not 1 <= self.max_signal_age_seconds <= 300:
            raise ValueError("信号有效期必须在 1 到 300 秒之间")
        if not 10 <= self.ai_history_days <= 90:
            raise ValueError("AI 历史走势天数必须在 10 到 90 之间")
        if not 1 <= self.ai_news_days <= 30:
            raise ValueError("AI 新闻回看天数必须在 1 到 30 之间")
        if not 1 <= self.ai_news_limit <= 20:
            raise ValueError("AI 新闻条数必须在 1 到 20 之间")
        if not 5 <= self.ai_timeout_seconds <= 60:
            raise ValueError("AI 请求超时必须在 5 到 60 秒之间")
        self.buy_notional = str(self.buy_notional)
        self.sell_quantity = str(self.sell_quantity)
        self.max_order_notional = str(self.max_order_notional)
        self.max_daily_buy_notional = str(self.max_daily_buy_notional)
        self.stop_loss_percent = str(self.stop_loss_percent)
        self.take_profit_percent = str(self.take_profit_percent)
        self.ai_min_confidence = str(self.ai_min_confidence)
        parsed_decimals: dict[str, Decimal] = {}
        for label, value in (
            ("开仓金额", self.buy_notional),
            ("卖出数量", self.sell_quantity),
            ("单笔金额上限", self.max_order_notional),
            ("账户每日开仓上限", self.max_daily_buy_notional),
            ("止损比例", self.stop_loss_percent),
            ("止盈比例", self.take_profit_percent),
        ):
            try:
                number = Decimal(value)
                if not number.is_finite() or number <= 0:
                    raise ValueError
                parsed_decimals[label] = number
            except (InvalidOperation, ValueError):
                raise ValueError(f"{label}必须是正数") from None
        if parsed_decimals["开仓金额"] > parsed_decimals["单笔金额上限"]:
            raise ValueError("开仓金额不能超过单笔金额上限")
        if parsed_decimals["开仓金额"] > parsed_decimals["账户每日开仓上限"]:
            raise ValueError("开仓金额不能超过账户每日开仓上限")
        for label in ("止损比例", "止盈比例"):
            if not Decimal("0.1") <= parsed_decimals[label] <= Decimal("50"):
                raise ValueError(f"{label}必须在 0.1% 到 50% 之间")
        try:
            confidence = Decimal(self.ai_min_confidence)
            if not confidence.is_finite():
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError("AI 最低置信度必须是 0.5 到 1 之间的数") from None
        if not Decimal("0.5") <= confidence <= Decimal("1"):
            raise ValueError("AI 最低置信度必须在 0.5 到 1 之间")
        rest_url = urlparse(self.rest_base_url)
        if rest_url.scheme != "https" or rest_url.hostname not in ALLOWED_REST_HOSTS:
            raise ValueError("REST 地址必须是 Binance 官方 HTTPS 地址")
        websocket_url = urlparse(self.websocket_base_url)
        if (
            websocket_url.scheme != "wss"
            or websocket_url.hostname not in ALLOWED_WS_HOSTS
        ):
            raise ValueError("WebSocket 地址必须是 Binance 官方 WSS 地址")


def normalize_symbols(symbols: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_symbol in symbols:
        if not isinstance(raw_symbol, str):
            raise ValueError("标的代码必须是字符串")
        symbol = raw_symbol.strip().upper()
        if not symbol:
            continue
        if SYMBOL_PATTERN.fullmatch(symbol) is None:
            raise ValueError(f"标的代码格式不正确: {symbol}")
        if symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def default_config_path() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if root:
        return Path(root) / "AutoQuant" / "config.json"
    return Path.home() / ".autoquant" / "config.json"


def credential_or_environment(configured_value: str, environment_name: str) -> str:
    """Prefer a configured credential and fall back to its environment variable."""
    configured = str(configured_value).strip()
    if configured:
        return configured
    return os.environ.get(environment_name, "").strip()


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_path()

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        try:
            payload: dict[str, Any] = json.loads(
                self.path.read_text(encoding="utf-8")
            )
            payload.pop("contract_multiplier", None)
            if "buy_notional" in payload and "max_order_notional" not in payload:
                legacy_buy = Decimal(str(payload["buy_notional"]))
                payload["max_order_notional"] = str(max(legacy_buy, Decimal("100")))
            if (
                "max_order_notional" in payload
                and "max_daily_buy_notional" not in payload
            ):
                payload["max_daily_buy_notional"] = str(
                    Decimal(str(payload["max_order_notional"])) * Decimal("3")
                )
            config = AppConfig(**payload)
            config.validate()
            return config
        except (
            OSError,
            json.JSONDecodeError,
            InvalidOperation,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(f"配置文件读取失败: {exc}") from exc

    def save(self, config: AppConfig) -> None:
        config.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

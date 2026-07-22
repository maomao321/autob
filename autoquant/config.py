from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_REST_URL = "https://api.binance.com"
DEFAULT_WS_URL = "wss://nbstream.binance.com/equity"


@dataclass(slots=True)
class AppConfig:
    symbols: list[str] = field(default_factory=lambda: ["AAPL"])
    provider: str = "binance_stocks"
    strategy: str = "five_minute_breakout"
    trading_mode: str = "PAPER"
    ma_period: int = 5
    buy_notional: str = "100.00"
    sell_quantity: str = "1"
    max_trades_per_day: int = 1
    rest_base_url: str = DEFAULT_REST_URL
    websocket_base_url: str = DEFAULT_WS_URL
    recv_window: int = 5000

    def validate(self) -> None:
        if not isinstance(self.symbols, list):
            raise ValueError("symbols 必须是股票代码列表")
        self.symbols = normalize_symbols(self.symbols)
        if not self.symbols:
            raise ValueError("至少配置一只股票")
        if self.provider != "binance_stocks":
            raise ValueError(f"暂不支持 API 供应商: {self.provider}")
        if self.strategy != "five_minute_breakout":
            raise ValueError(f"暂不支持策略: {self.strategy}")
        self.trading_mode = self.trading_mode.upper()
        if self.trading_mode not in {"PAPER", "REAL"}:
            raise ValueError("交易模式必须是 PAPER 或 REAL")
        try:
            self.ma_period = int(self.ma_period)
            self.max_trades_per_day = int(self.max_trades_per_day)
            self.recv_window = int(self.recv_window)
        except (TypeError, ValueError):
            raise ValueError("MA、每日交易次数和 recvWindow 必须是整数") from None
        if not 2 <= self.ma_period <= 200:
            raise ValueError("MA 周期必须在 2 到 200 之间")
        if not 1 <= self.max_trades_per_day <= 100:
            raise ValueError("每日最多交易次数必须在 1 到 100 之间")
        if not 1 <= self.recv_window <= 60000:
            raise ValueError("recvWindow 必须在 1 到 60000 毫秒之间")
        self.buy_notional = str(self.buy_notional)
        self.sell_quantity = str(self.sell_quantity)
        for label, value in (
            ("买入金额", self.buy_notional),
            ("卖出数量", self.sell_quantity),
        ):
            try:
                if Decimal(value) <= 0:
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise ValueError(f"{label}必须是正数") from None
        if not self.rest_base_url.startswith("https://"):
            raise ValueError("REST 地址必须使用 https://")
        if not self.websocket_base_url.startswith("wss://"):
            raise ValueError("WebSocket 地址必须使用 wss://")


def normalize_symbols(symbols: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_symbol in symbols:
        symbol = raw_symbol.strip().upper()
        if not symbol:
            continue
        if not symbol.replace(".", "").replace("-", "").isalnum():
            raise ValueError(f"股票代码格式不正确: {symbol}")
        if symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def default_config_path() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if root:
        return Path(root) / "AutoQuant" / "config.json"
    return Path.home() / ".autoquant" / "config.json"


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
            config = AppConfig(**payload)
            config.validate()
            return config
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
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

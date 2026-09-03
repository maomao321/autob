from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import quote, urlencode
from xml.etree import ElementTree

from autoquant_backend.ai_decision.constants import (
    DIRECTION_DAILY_BAR_COUNT,
    DIRECTION_DAILY_MAX_AGE_DAYS,
    GOOGLE_NEWS_URL,
    NASDAQ_HISTORICAL_URL,
)
from autoquant_backend.ai_decision.models import (
    DecisionError,
    HistoricalBarsFetcher,
    HistoricalSymbolResolver,
)
from autoquant_backend.ai_decision.sanitizing import _clean_text, _safe_error
from autoquant_backend.ai_decision.transport import _get_bytes
from autoquant_shared.formatting import financial_text
from autoquant_shared.models import Bar


class PublicMarketContextCollector:
    """Fetch news and provider-first price history for the AI context."""

    def __init__(
        self,
        history_days: int,
        news_days: int,
        news_limit: int,
        timeout_seconds: int,
        benchmarks: tuple[str, ...] = ("SPY", "QQQ"),
        get_bytes: Callable[[str, int], bytes] | None = None,
        historical_bars_fetcher: HistoricalBarsFetcher | None = None,
        historical_source_name: str = "",
        historical_symbol_resolver: HistoricalSymbolResolver | None = None,
    ) -> None:
        # Kept in the constructor for saved-config compatibility. The model
        # contract intentionally uses a fixed 30-bar daily window.
        self.history_days = DIRECTION_DAILY_BAR_COUNT
        self.news_days = news_days
        self.news_limit = news_limit
        self.timeout_seconds = timeout_seconds
        self.benchmarks = benchmarks
        self._get_bytes = get_bytes or _get_bytes
        self._historical_bars_fetcher = historical_bars_fetcher
        self._historical_source_name = (
            historical_source_name.strip() or "API provider"
        )
        self._historical_symbol_resolver = historical_symbol_resolver
        self._symbol_lock = threading.Lock()
        self._trading_symbols: dict[str, str] = {}

    def set_trading_symbol(
        self, market_data_symbol: str, trading_symbol: str
    ) -> None:
        market_data_symbol = market_data_symbol.strip().upper()
        trading_symbol = trading_symbol.strip().upper()
        if not market_data_symbol or not trading_symbol:
            return
        with self._symbol_lock:
            self._trading_symbols[market_data_symbol] = trading_symbol

    def collect(self, symbol: str, current_daily_bar: Bar) -> dict[str, Any]:
        symbol = symbol.upper()
        requested = (symbol,) + self.benchmarks
        trends: dict[str, dict[str, Any]] = {}
        trend_sources: dict[str, str] = {}
        failures: list[str] = []
        news: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=len(requested) + 1) as executor:
            trend_futures = {
                executor.submit(self._fetch_trend, ticker): ticker
                for ticker in requested
            }
            news_future = executor.submit(self._fetch_news, symbol)
            for future in as_completed(trend_futures):
                ticker = trend_futures[future]
                try:
                    trend, source, warning = future.result()
                    trends[ticker] = trend
                    trend_sources[ticker] = source
                    if warning:
                        failures.append(f"{ticker}走势: {warning}")
                except Exception as exc:
                    failures.append(f"{ticker}走势: {_safe_error(exc)}")
            try:
                news = news_future.result()
            except Exception as exc:
                failures.append(f"新闻: {_safe_error(exc)}")

        if symbol not in trends:
            raise DecisionError("个股近期走势不可用")
        broad_market = {
            benchmark: trends[benchmark]
            for benchmark in self.benchmarks
            if benchmark in trends
        }
        if not broad_market:
            raise DecisionError("大盘近期走势不可用")

        used_sources = tuple(dict.fromkeys(trend_sources.values()))
        price_source = (
            used_sources[0]
            if len(used_sources) == 1
            else "mixed: " + ", ".join(used_sources)
        )

        return {
            "as_of_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": symbol,
            "current_session": _bar_payload(current_daily_bar),
            "symbol_trend": trends[symbol],
            "broad_market_trends": broad_market,
            "recent_news": news,
            "data_quality": {
                "warnings": failures + ([] if news else ["未获取到近期新闻"]),
                "news_count": len(news),
                "price_source": price_source,
                "price_sources": trend_sources,
                "news_source": "Google News RSS",
            },
        }

    def _fetch_trend(self, symbol: str) -> tuple[dict[str, Any], str, str]:
        primary_error = ""
        if self._historical_bars_fetcher is not None:
            try:
                return (
                    self._fetch_provider_trend(symbol),
                    self._historical_source_name,
                    "",
                )
            except Exception as exc:
                primary_error = _safe_error(exc)

        try:
            trend = self._fetch_nasdaq_trend(symbol)
        except Exception as exc:
            if primary_error:
                raise DecisionError(
                    f"{self._historical_source_name} 失败: {primary_error}；"
                    f"Nasdaq 兜底失败: {_safe_error(exc)}"
                ) from exc
            raise
        warning = (
            f"{self._historical_source_name} 失败，已使用 Nasdaq 兜底: "
            f"{primary_error}"
            if primary_error
            else ""
        )
        return trend, "Nasdaq public historical endpoint", warning

    def _fetch_provider_trend(self, symbol: str) -> dict[str, Any]:
        fetcher = self._historical_bars_fetcher
        if fetcher is None:
            raise DecisionError("未配置 API 供应商历史 K 线接口")
        with self._symbol_lock:
            provider_symbol = self._trading_symbols.get(symbol, "")
        if not provider_symbol:
            provider_symbol = symbol
            if self._historical_symbol_resolver is not None:
                provider_symbol = self._historical_symbol_resolver(symbol)
        provider_symbol = provider_symbol.strip().upper()
        if not provider_symbol:
            raise DecisionError(f"{symbol} 无法映射到 API 供应商交易代码")
        now_ms = int(time.time() * 1000)
        # End at the latest completed UTC daily candle. Using the current
        # instant would make Binance include today's open candle in limit=30;
        # the closed-bar filter would then leave only 29 usable candles.
        day_ms = 86_400_000
        end_time = now_ms - now_ms % day_ms - 1
        # Omitting start_time makes provider APIs return the latest bars at or
        # before end_time instead of the first bars in a lookback range.
        bars = fetcher(
            provider_symbol,
            "1d",
            None,
            end_time,
            self.history_days,
        )
        valid_bars = {
            bar.open_time: bar
            for bar in bars
            if isinstance(bar, Bar)
            and bar.interval == "1d"
            and bar.closed
            and bar.open_time <= end_time
            and _valid_bar_prices(bar)
        }
        ordered = sorted(valid_bars.values(), key=lambda bar: bar.open_time)
        if len(ordered) < self.history_days:
            raise DecisionError(
                f"{provider_symbol} 有效日线少于 {self.history_days} 根"
            )
        latest_age_ms = end_time - ordered[-1].close_time
        max_age_ms = DIRECTION_DAILY_MAX_AGE_DAYS * 86_400_000
        if latest_age_ms > max_age_ms:
            latest_date = datetime.fromtimestamp(
                ordered[-1].open_time / 1000, tz=timezone.utc
            ).date().isoformat()
            raise DecisionError(
                f"{provider_symbol} 最新日线已过期（{latest_date}）"
            )
        points = [
            (
                datetime.fromtimestamp(
                    bar.open_time / 1000, tz=timezone.utc
                ).date().isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
            )
            for bar in ordered[-self.history_days :]
        ]
        return _trend_payload(symbol, points)

    def _fetch_nasdaq_trend(self, symbol: str) -> dict[str, Any]:
        today = date.today()
        calendar_days = max(self.history_days * 2, 45)
        params = {
            "fromdate": (today - timedelta(days=calendar_days)).isoformat(),
            "todate": today.isoformat(),
            "limit": str(self.history_days),
        }
        preferred = "etf" if symbol in self.benchmarks else "stocks"
        asset_classes = (preferred, "stocks" if preferred == "etf" else "etf")
        points: list[tuple[str, Decimal, Decimal, Decimal, Decimal]] = []
        for asset_class in asset_classes:
            query = urlencode({"assetclass": asset_class, **params})
            content = self._get_bytes(
                f"{NASDAQ_HISTORICAL_URL}/{quote(symbol)}/historical?{query}",
                self.timeout_seconds,
            )
            points = _parse_nasdaq_points(content)
            if points:
                break
        points.sort(key=lambda item: item[0])
        points = points[-self.history_days :]
        if len(points) < self.history_days:
            raise DecisionError(
                f"{symbol} 有效日线少于 {self.history_days} 根"
            )
        return _trend_payload(symbol, points)

    def _fetch_news(self, symbol: str) -> list[dict[str, str]]:
        query = quote(f"{symbol} stock when:{self.news_days}d")
        url = (
            f"{GOOGLE_NEWS_URL}?q={query}&hl=en-US&gl=US&ceid=US%3Aen"
        )
        content = self._get_bytes(url, self.timeout_seconds)
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise DecisionError("新闻 RSS 格式错误") from exc
        result: list[dict[str, str]] = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.news_days + 1)
        for item in root.findall("./channel/item"):
            title = _clean_text(item.findtext("title") or "", 300)
            link = _clean_text(item.findtext("link") or "", 500)
            source = _clean_text(item.findtext("source") or "", 120)
            published_raw = item.findtext("pubDate") or ""
            try:
                published = parsedate_to_datetime(published_raw)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                published = published.astimezone(timezone.utc)
                if published < cutoff:
                    continue
                published_text = published.isoformat(timespec="seconds")
            except (TypeError, ValueError, OverflowError):
                published_text = _clean_text(published_raw, 80)
            if not title:
                continue
            result.append(
                {
                    "title": title,
                    "source": source,
                    "published_utc": published_text,
                    "url": link,
                }
            )
            if len(result) >= self.news_limit:
                break
        return result


def _parse_nasdaq_points(
    content: bytes,
) -> list[tuple[str, Decimal, Decimal, Decimal, Decimal]]:
    try:
        payload = json.loads(content.decode("utf-8"))
        data = payload.get("data")
        table = data.get("tradesTable") if isinstance(data, dict) else None
        rows = table.get("rows") if isinstance(table, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
        raise DecisionError("Nasdaq 历史行情格式错误") from exc
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise DecisionError("Nasdaq 历史行情 rows 格式错误")
    points: list[tuple[str, Decimal, Decimal, Decimal, Decimal]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            day = datetime.strptime(str(row.get("date", "")), "%m/%d/%Y")
            prices = tuple(
                Decimal(
                    str(row.get(field, "")).replace("$", "").replace(",", "")
                )
                for field in ("open", "high", "low", "close")
            )
            open_price, high, low, close = prices
            if (
                all(value.is_finite() and value > 0 for value in prices)
                and low <= min(open_price, close)
                and high >= max(open_price, close)
                and low <= high
            ):
                points.append(
                    (day.date().isoformat(), open_price, high, low, close)
                )
        except (InvalidOperation, TypeError, ValueError):
            continue
    return points


def _trend_payload(
    symbol: str,
    points: list[tuple[str, Decimal, Decimal, Decimal, Decimal]],
) -> dict[str, Any]:
    closes = [close for _day, _open, _high, _low, close in points]

    def change(period: int) -> str | None:
        if len(closes) <= period or closes[-period - 1] <= 0:
            return None
        value = (closes[-1] / closes[-period - 1] - Decimal("1")) * Decimal(
            "100"
        )
        return format(value.quantize(Decimal("0.01")), "f")

    def mean(period: int) -> str | None:
        if len(closes) < period:
            return None
        value = sum(closes[-period:], Decimal("0")) / Decimal(period)
        return format(value.quantize(Decimal("0.01")), "f")

    return {
        "symbol": symbol,
        "observations": len(points),
        "first_date": points[0][0],
        "latest_date": points[-1][0],
        "latest_close": financial_text(closes[-1]),
        "change_1d_percent": change(1),
        "change_5d_percent": change(5),
        "change_20d_percent": change(20),
        "sma_5": mean(5),
        "sma_20": mean(20),
        "daily_bars": [
            {
                "date": day,
                "open": financial_text(open_price),
                "high": financial_text(high),
                "low": financial_text(low),
                "close": financial_text(close),
            }
            for day, open_price, high, low, close in points
        ],
    }


def _bar_payload(bar: Bar) -> dict[str, Any]:
    return {
        "open_time_ms": bar.open_time,
        "close_time_ms": bar.close_time,
        "open": financial_text(bar.open),
        "high": financial_text(bar.high),
        "low": financial_text(bar.low),
        "close": financial_text(bar.close),
        "is_closed": bar.closed,
    }


def _valid_bar_prices(bar: Bar) -> bool:
    prices = (bar.open, bar.high, bar.low, bar.close)
    return (
        all(value.is_finite() and value > 0 for value in prices)
        and bar.low <= min(bar.open, bar.close)
        and bar.high >= max(bar.open, bar.close)
        and bar.low <= bar.high
    )

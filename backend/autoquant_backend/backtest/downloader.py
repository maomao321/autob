from __future__ import annotations

import threading
import time
from typing import Callable

from autoquant_backend.backtest.models import (
    DAY_MS,
    DOWNLOAD_DAYS,
    DOWNLOAD_INTERVALS,
    INTERVAL_MS,
)
from autoquant_backend.backtest.store import BacktestStore
from autoquant_backend.providers.base import TradingProvider

class HistoricalDownloader:
    def __init__(
        self,
        store: BacktestStore,
        provider_factory: Callable[[], TradingProvider],
        provider_name: str,
    ) -> None:
        self.store = store
        self.provider_factory = provider_factory
        self.provider_name = provider_name

    def start(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("回测标的不能为空")
        if self.provider_name != "binance_futures":
            raise ValueError("180 天 1 分钟历史下载当前仅支持 Binance Futures")
        if self.store.has_active_download(self.provider_name, normalized):
            raise ValueError(f"{normalized} 已有历史 K 线下载任务在执行")
        end_time = int(time.time() * 1000) - 1
        start_time = end_time - DOWNLOAD_DAYS * DAY_MS
        download_id = self.store.create_download(
            self.provider_name, normalized, start_time, end_time
        )
        threading.Thread(
            target=self._run,
            args=(download_id, normalized, start_time, end_time),
            name=f"backtest-download-{normalized}",
            daemon=True,
        ).start()
        return download_id

    def _run(
        self,
        download_id: str,
        symbol: str,
        start_time: int,
        end_time: int,
    ) -> None:
        try:
            provider = self.provider_factory()
            counts: dict[str, int] = {}
            self.store.update_download(
                download_id, status="RUNNING", message="正在下载历史 K 线"
            )
            for index, interval in enumerate(DOWNLOAD_INTERVALS):
                count = self._download_interval(
                    download_id,
                    provider,
                    symbol,
                    interval,
                    start_time,
                    end_time,
                    index,
                )
                if count <= 0:
                    raise RuntimeError(f"{symbol} 未下载到 {interval} K 线")
                counts[interval] = count
            self.store.update_download(
                download_id,
                status="COMPLETED",
                current_interval="",
                progress=100,
                daily_count=counts["1d"],
                five_minute_count=counts["5m"],
                one_minute_count=counts["1m"],
                message=(
                    "最近 180 天范围内可用的日线、5 分钟和 1 分钟 K 线"
                    "下载完成，数量以行情源实际返回为准"
                ),
                completed_at=int(time.time() * 1000),
            )
        except Exception as exc:
            self.store.update_download(
                download_id,
                status="FAILED",
                message=str(exc) or exc.__class__.__name__,
                completed_at=int(time.time() * 1000),
            )

    def _download_interval(
        self,
        download_id: str,
        provider: TradingProvider,
        symbol: str,
        interval: str,
        start_time: int,
        end_time: int,
        interval_index: int,
    ) -> int:
        step = INTERVAL_MS[interval]
        latest_open_time = self.store.latest_bar_open_time(
            self.provider_name, symbol, interval, start_time, end_time
        )
        cursor = (
            start_time
            if latest_open_time is None
            else max(start_time, latest_open_time + step)
        )
        pages = 0
        while cursor <= end_time:
            bars = provider.get_historical_bars(
                symbol, interval, cursor, end_time, 1500
            )
            eligible = [
                bar
                for bar in bars
                if bar.closed and cursor <= bar.open_time <= end_time
            ]
            if not eligible:
                break
            eligible.sort(key=lambda bar: bar.open_time)
            self.store.upsert_bars(self.provider_name, eligible)
            next_cursor = eligible[-1].open_time + step
            if next_cursor <= cursor:
                raise RuntimeError(f"{interval} 历史行情分页未向前推进")
            cursor = next_cursor
            pages += 1
            completed_fraction = min(
                1.0, max(0.0, (cursor - start_time) / (end_time - start_time))
            )
            progress = int((interval_index + completed_fraction) / 3 * 100)
            count = self.store.count_bars(
                self.provider_name, symbol, interval, start_time, end_time
            )
            field = {
                "1d": "daily_count",
                "5m": "five_minute_count",
                "1m": "one_minute_count",
            }[interval]
            self.store.update_download(
                download_id,
                current_interval=interval,
                progress=min(progress, 99),
                message=f"正在下载 {interval} K 线，已持久化 {count} 根",
                **{field: count},
            )
            if len(eligible) < 1500:
                break
            if pages > 1000:
                raise RuntimeError(f"{interval} 历史行情分页次数异常")
        return self.store.count_bars(
            self.provider_name, symbol, interval, start_time, end_time
        )




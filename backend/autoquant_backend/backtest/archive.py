from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from collections.abc import Iterator
from contextlib import closing
from decimal import Decimal
from typing import Any

from autoquant_backend.backtest.models import (
    ARCHIVE_FILES,
    ARCHIVE_MAX_UNCOMPRESSED_BYTES,
    ARCHIVE_VERSION,
    BAR_CSV_FIELDS,
    DOWNLOAD_INTERVALS,
    SYMBOL_RE,
)
from autoquant_backend.backtest.store import BacktestStore
from autoquant_shared.models import Bar

class HistoricalArchiveService:
    """Export and import portable, validated per-symbol historical datasets."""

    def __init__(self, store: BacktestStore) -> None:
        self.store = store

    def export(self, provider: str, symbol: str) -> tuple[bytes, str]:
        provider = provider.strip().lower()
        symbol = self._symbol(symbol)
        counts: dict[str, int] = {}
        ranges: dict[str, dict[str, int]] = {}
        with self.store._lock, closing(self.store._connect()) as connection:
            for interval in DOWNLOAD_INTERVALS:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count, MIN(open_time) AS start_time,
                           MAX(close_time) AS end_time
                    FROM market_bars
                    WHERE provider = ? AND symbol = ? AND interval = ?
                    """,
                    (provider, symbol, interval),
                ).fetchone()
                count = int(row["count"] if row else 0)
                counts[interval] = count
                ranges[interval] = {
                    "start_time": int(row["start_time"] or 0) if row else 0,
                    "end_time": int(row["end_time"] or 0) if row else 0,
                }
            if not any(counts.values()):
                raise ValueError(f"{symbol} 没有可导出的持久化历史 K 线")

            manifest = {
                "format": "autoquant-historical-klines",
                "version": ARCHIVE_VERSION,
                "provider": provider,
                "symbol": symbol,
                "exported_at": int(time.time() * 1000),
                "counts": counts,
                "ranges": ranges,
            }
            output = io.BytesIO()
            with zipfile.ZipFile(
                output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                )
                for interval in DOWNLOAD_INTERVALS:
                    with archive.open(ARCHIVE_FILES[interval], "w") as raw:
                        text = io.TextIOWrapper(
                            raw, encoding="utf-8", newline="", write_through=True
                        )
                        writer = csv.writer(text, lineterminator="\n")
                        writer.writerow(BAR_CSV_FIELDS)
                        cursor = connection.execute(
                            """
                            SELECT open_time, close_time, open, high, low, close,
                                   volume
                            FROM market_bars
                            WHERE provider = ? AND symbol = ? AND interval = ?
                            ORDER BY open_time
                            """,
                            (provider, symbol, interval),
                        )
                        while True:
                            rows = cursor.fetchmany(5000)
                            if not rows:
                                break
                            writer.writerows(tuple(row[field] for field in BAR_CSV_FIELDS) for row in rows)
                        text.detach()
        filename = f"{symbol}_{provider}_historical_klines.zip"
        return output.getvalue(), filename

    def import_archive(
        self, payload: bytes, *, expected_symbol: str = ""
    ) -> dict[str, Any]:
        if not payload:
            raise ValueError("导入文件为空")
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload), mode="r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValueError("导入文件不是有效的 AutoQuant K 线 ZIP 数据包") from exc
        with archive:
            infos = archive.infolist()
            names = set(archive.namelist())
            if len(names) != len(infos):
                raise ValueError("K 线数据包包含重复文件名")
            if any(info.flag_bits & 0x1 for info in infos):
                raise ValueError("K 线数据包不能包含加密文件")
            required = {"manifest.json", *ARCHIVE_FILES.values()}
            if not required.issubset(names):
                missing = sorted(required - names)
                raise ValueError("K 线数据包缺少文件: " + ", ".join(missing))
            if any(
                name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/")
                for name in names
            ):
                raise ValueError("K 线数据包包含不安全的文件路径")
            total_size = sum(info.file_size for info in infos)
            if total_size > ARCHIVE_MAX_UNCOMPRESSED_BYTES:
                raise ValueError("K 线数据包解压后超过 512 MB 限制")
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > 1_000_000:
                raise ValueError("K 线数据包清单超过 1 MB 限制")
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
                raise ValueError("K 线数据包清单无效") from exc
            if not isinstance(manifest, dict):
                raise ValueError("K 线数据包清单必须是 JSON 对象")
            if manifest.get("format") != "autoquant-historical-klines":
                raise ValueError("不支持的 K 线数据包格式")
            if int(manifest.get("version", 0)) != ARCHIVE_VERSION:
                raise ValueError("不支持的 K 线数据包版本")
            provider = str(manifest.get("provider", "")).strip().lower()
            if provider not in {"binance_stocks", "binance_futures"}:
                raise ValueError("K 线数据包行情源不受支持")
            symbol = self._symbol(str(manifest.get("symbol", "")))
            selected_symbol = expected_symbol.strip().upper()
            if selected_symbol and self._symbol(selected_symbol) != symbol:
                raise ValueError(
                    f"数据包标的 {symbol} 与页面指定标的 {selected_symbol} 不一致"
                )
            raw_counts = manifest.get("counts", {})
            if not isinstance(raw_counts, dict):
                raise ValueError("K 线数据包 counts 清单无效")
            expected_counts = {
                interval: self._nonnegative_int(raw_counts.get(interval, 0), "K 线数量")
                for interval in DOWNLOAD_INTERVALS
            }
            counts = {interval: 0 for interval in DOWNLOAD_INTERVALS}
            range_state: dict[str, int | None] = {"start": None, "end": 0}

            def bars() -> Iterator[Bar]:
                for interval in DOWNLOAD_INTERVALS:
                    previous_open_time = -1
                    with archive.open(ARCHIVE_FILES[interval], "r") as raw:
                        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                        reader = csv.DictReader(text)
                        if tuple(reader.fieldnames or ()) != BAR_CSV_FIELDS:
                            raise ValueError(f"{interval} CSV 表头不正确")
                        for row_number, row in enumerate(reader, start=2):
                            try:
                                bar = self._parse_bar(symbol, interval, row)
                            except (ArithmeticError, TypeError, ValueError) as exc:
                                raise ValueError(
                                    f"{interval} CSV 第 {row_number} 行无效: {exc}"
                                ) from exc
                            if bar.open_time <= previous_open_time:
                                raise ValueError(
                                    f"{interval} CSV 第 {row_number} 行时间重复或未递增"
                                )
                            previous_open_time = bar.open_time
                            counts[interval] += 1
                            if range_state["start"] is None:
                                range_state["start"] = bar.open_time
                            else:
                                range_state["start"] = min(
                                    range_state["start"], bar.open_time
                                )
                            range_state["end"] = max(
                                int(range_state["end"] or 0), bar.close_time
                            )
                            yield bar
                    if counts[interval] != expected_counts[interval]:
                        raise ValueError(
                            f"{interval} CSV 实际 {counts[interval]} 根，"
                            f"与清单 {expected_counts[interval]} 根不一致"
                        )

            imported = self.store.import_bars(provider, bars())
            if (
                imported <= 0
                or range_state["start"] is None
                or int(range_state["end"] or 0) <= range_state["start"]
            ):
                raise ValueError("K 线数据包没有可导入的历史数据")
            download_id = self.store.record_import(
                provider,
                symbol,
                range_state["start"],
                int(range_state["end"] or 0),
                counts,
            )
        return {
            "download_id": download_id,
            "provider": provider,
            "symbol": symbol,
            "counts": counts,
            "imported_rows": imported,
            "start_time": range_state["start"],
            "end_time": int(range_state["end"] or 0),
        }

    @staticmethod
    def _symbol(value: str) -> str:
        symbol = value.strip().upper()
        if SYMBOL_RE.fullmatch(symbol) is None:
            raise ValueError("历史 K 线标的格式不正确")
        return symbol

    @staticmethod
    def _nonnegative_int(value: Any, label: str) -> int:
        parsed = int(value)
        if parsed < 0:
            raise ValueError(f"{label}不能为负数")
        return parsed

    @staticmethod
    def _parse_bar(
        symbol: str, interval: str, row: dict[str, Any]
    ) -> Bar:
        if None in row:
            raise ValueError("CSV 包含未声明的额外列")
        open_time = int(row["open_time"])
        close_time = int(row["close_time"])
        prices = {
            field: Decimal(str(row[field]))
            for field in ("open", "high", "low", "close", "volume")
        }
        if open_time < 0 or close_time <= open_time:
            raise ValueError("K 线时间范围不正确")
        if any(not value.is_finite() for value in prices.values()):
            raise ValueError("OHLCV 必须是有限数字")
        if any(prices[field] <= 0 for field in ("open", "high", "low", "close")):
            raise ValueError("OHLC 必须为正数")
        if prices["volume"] < 0:
            raise ValueError("成交量不能为负数")
        if prices["high"] < max(prices["open"], prices["close"], prices["low"]):
            raise ValueError("最高价小于其他价格")
        if prices["low"] > min(prices["open"], prices["close"], prices["high"]):
            raise ValueError("最低价大于其他价格")
        return Bar(
            symbol=symbol,
            interval=interval,
            open_time=open_time,
            close_time=close_time,
            open=prices["open"],
            high=prices["high"],
            low=prices["low"],
            close=prices["close"],
            volume=prices["volume"],
            closed=True,
        )




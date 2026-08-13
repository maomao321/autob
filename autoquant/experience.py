from __future__ import annotations

import csv
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from autoquant.config import default_config_path
from autoquant.state import OrderRecord


OPENAI_FILES_URL = "https://api.openai.com/v1/files"
OPENAI_VECTOR_STORES_URL = "https://api.openai.com/v1/vector_stores"
MAX_HTTP_RESPONSE_BYTES = 2_000_000
MAX_KLINE_FILE_BYTES = 50_000_000
MAX_KLINE_ROWS = 500_000


class ExperienceError(RuntimeError):
    """A safe, user-displayable experience extraction or upload failure."""


@dataclass(frozen=True, slots=True)
class MarketBar:
    symbol: str
    timestamp_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")
    interval: str = ""


@dataclass(frozen=True, slots=True)
class TradeExperience:
    experience_id: str
    symbol: str
    paper: bool
    outcome: str
    entry_order_id: str
    exit_order_id: str
    entry_time_ms: int
    exit_time_ms: int
    entry_time: str
    exit_time: str
    quantity: str
    entry_price: str
    exit_price: str
    entry_fee: str
    exit_fee: str
    gross_pnl: str
    net_pnl: str
    return_percent: str
    holding_seconds: int
    source: str = "order_ledger"
    pre_entry_pattern: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExperienceSummary:
    total: int
    wins: int
    losses: int
    breakeven: int
    with_kline: int
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class UploadResult:
    vector_store_id: str
    file_id: str
    attachment_id: str
    status: str


def default_experience_path() -> Path:
    return default_config_path().with_name("trade_experiences.json")


def extract_trade_experiences(
    records: Iterable[OrderRecord],
    *,
    bars_by_symbol: dict[str, list[MarketBar]] | None = None,
    pattern_bars: int = 20,
) -> list[TradeExperience]:
    if not 5 <= pattern_bars <= 240:
        raise ExperienceError("K线形态窗口必须在 5 到 240 根之间")
    bars_by_symbol = bars_by_symbol or {}
    ordered = sorted(records, key=lambda item: (item.created_at, item.client_order_id))
    lots: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    experiences: list[TradeExperience] = []

    for record in ordered:
        quantity = record.filled_quantity
        price = record.average_price
        if quantity <= 0 or price <= 0:
            continue
        key = (record.symbol.upper(), record.paper)
        symbol_lots = lots.setdefault(key, [])
        if record.side == "BUY":
            symbol_lots.append(
                {
                    "record": record,
                    "quantity": quantity,
                    "fee": max(record.fee, Decimal("0")),
                }
            )
            continue
        if record.side != "SELL":
            continue

        sell_remaining = quantity
        sell_fee_total = max(record.fee, Decimal("0"))
        sell_match_index = 0
        while sell_remaining > 0 and symbol_lots:
            lot = symbol_lots[0]
            lot_quantity: Decimal = lot["quantity"]
            matched = min(sell_remaining, lot_quantity)
            if matched <= 0:
                symbol_lots.pop(0)
                continue
            entry_record: OrderRecord = lot["record"]
            entry_fee_remaining: Decimal = lot["fee"]
            entry_fee = (
                entry_fee_remaining * matched / lot_quantity
                if lot_quantity > 0
                else Decimal("0")
            )
            exit_fee = sell_fee_total * matched / quantity
            gross_pnl = (record.average_price - entry_record.average_price) * matched
            net_pnl = gross_pnl - entry_fee - exit_fee
            entry_cost = entry_record.average_price * matched + entry_fee
            return_percent = (
                net_pnl / entry_cost * Decimal("100")
                if entry_cost > 0
                else Decimal("0")
            )
            outcome = "WIN" if net_pnl > 0 else "LOSS" if net_pnl < 0 else "BREAKEVEN"
            sell_match_index += 1
            pattern = extract_pre_entry_pattern(
                bars_by_symbol.get(key[0], []),
                entry_record.created_at,
                pattern_bars,
            )
            notes = tuple(
                text
                for text in (
                    _clean_note(entry_record.message),
                    _clean_note(record.message),
                )
                if text
            )
            experiences.append(
                TradeExperience(
                    experience_id=(
                        f"{entry_record.client_order_id}__"
                        f"{record.client_order_id}__{sell_match_index}"
                    ),
                    symbol=key[0],
                    paper=record.paper,
                    outcome=outcome,
                    entry_order_id=entry_record.client_order_id,
                    exit_order_id=record.client_order_id,
                    entry_time_ms=entry_record.created_at,
                    exit_time_ms=record.created_at,
                    entry_time=_iso_time(entry_record.created_at),
                    exit_time=_iso_time(record.created_at),
                    quantity=_decimal_text(matched),
                    entry_price=_decimal_text(entry_record.average_price),
                    exit_price=_decimal_text(record.average_price),
                    entry_fee=_decimal_text(entry_fee),
                    exit_fee=_decimal_text(exit_fee),
                    gross_pnl=_decimal_text(gross_pnl),
                    net_pnl=_decimal_text(net_pnl),
                    return_percent=_decimal_text(return_percent, places=6),
                    holding_seconds=max(
                        0, (record.created_at - entry_record.created_at) // 1000
                    ),
                    pre_entry_pattern=pattern,
                    notes=notes,
                )
            )
            sell_remaining -= matched
            lot["quantity"] = lot_quantity - matched
            lot["fee"] = entry_fee_remaining - entry_fee
            if lot["quantity"] <= 0:
                symbol_lots.pop(0)

    return experiences


def summarize_experiences(
    experiences: Iterable[TradeExperience],
) -> ExperienceSummary:
    items = list(experiences)
    return ExperienceSummary(
        total=len(items),
        wins=sum(item.outcome == "WIN" for item in items),
        losses=sum(item.outcome == "LOSS" for item in items),
        breakeven=sum(item.outcome == "BREAKEVEN" for item in items),
        with_kline=sum(
            bool(item.pre_entry_pattern.get("available")) for item in items
        ),
        net_pnl=sum((Decimal(item.net_pnl) for item in items), Decimal("0")),
    )


def load_ohlcv_csv(path: Path) -> dict[str, list[MarketBar]]:
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ExperienceError(f"无法读取K线文件：{exc}") from exc
    if size > MAX_KLINE_FILE_BYTES:
        raise ExperienceError("K线文件不能超过 50 MB")

    result: dict[str, list[MarketBar]] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ExperienceError("K线CSV缺少表头")
            columns = {name.strip().lower(): name for name in reader.fieldnames}
            timestamp_name = next(
                (
                    name
                    for name in (
                        "close_time",
                        "close_time_ms",
                        "timestamp",
                        "time",
                        "datetime",
                        "date",
                        "open_time",
                        "open_time_ms",
                    )
                    if name in columns
                ),
                None,
            )
            timestamp_key = columns[timestamp_name] if timestamp_name else None
            required = ("symbol", "open", "high", "low", "close")
            missing = [name for name in required if name not in columns]
            if timestamp_key is None:
                missing.append("timestamp")
            if missing:
                raise ExperienceError("K线CSV缺少字段：" + ", ".join(missing))
            volume_key = columns.get("volume")
            interval_key = columns.get("interval")
            for row_number, row in enumerate(reader, start=2):
                if row_number > MAX_KLINE_ROWS + 1:
                    raise ExperienceError("K线CSV不能超过 500000 行")
                try:
                    symbol = str(row[columns["symbol"]]).strip().upper()
                    if not symbol:
                        raise ValueError("symbol为空")
                    values = {
                        name: Decimal(str(row[columns[name]]).strip())
                        for name in ("open", "high", "low", "close")
                    }
                    volume = (
                        Decimal(str(row[volume_key]).strip() or "0")
                        if volume_key
                        else Decimal("0")
                    )
                    if any(
                        not value.is_finite() or value <= 0
                        for value in values.values()
                    ):
                        raise ValueError("价格必须为正数")
                    if not volume.is_finite() or volume < 0:
                        raise ValueError("成交量不能为负数")
                    if values["high"] < max(values["open"], values["close"]):
                        raise ValueError("最高价低于开盘价或收盘价")
                    if values["low"] > min(values["open"], values["close"]):
                        raise ValueError("最低价高于开盘价或收盘价")
                    interval = (
                        str(row[interval_key]).strip() if interval_key else ""
                    )
                    timestamp_ms = _parse_timestamp_ms(str(row[timestamp_key]))
                    if timestamp_name in {"open_time", "open_time_ms"}:
                        if not interval:
                            raise ValueError("使用open_time时必须提供interval")
                        timestamp_ms += _parse_interval_ms(interval)
                    bar = MarketBar(
                        symbol=symbol,
                        timestamp_ms=timestamp_ms,
                        open=values["open"],
                        high=values["high"],
                        low=values["low"],
                        close=values["close"],
                        volume=volume,
                        interval=interval,
                    )
                except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                    raise ExperienceError(
                        f"K线CSV第 {row_number} 行格式错误：{exc}"
                    ) from exc
                result.setdefault(symbol, []).append(bar)
    except UnicodeDecodeError as exc:
        raise ExperienceError("K线CSV必须使用 UTF-8 编码") from exc
    except OSError as exc:
        raise ExperienceError(f"读取K线CSV失败：{exc}") from exc

    for bars in result.values():
        bars.sort(key=lambda item: item.timestamp_ms)
    return result


def extract_pre_entry_pattern(
    bars: Iterable[MarketBar], entry_time_ms: int, window: int
) -> dict[str, Any]:
    eligible = [bar for bar in bars if bar.timestamp_ms < entry_time_ms]
    selected = eligible[-window:]
    if not selected:
        return {
            "available": False,
            "bar_count": 0,
            "reason": "没有开仓前K线数据",
        }
    base = selected[0].close
    if base <= 0:
        return {
            "available": False,
            "bar_count": 0,
            "reason": "K线基准价格无效",
        }
    average_volume = sum((bar.volume for bar in selected), Decimal("0")) / Decimal(
        len(selected)
    )

    def pct(value: Decimal) -> str:
        return _decimal_text((value / base - Decimal("1")) * Decimal("100"), 6)

    normalized = []
    for bar in selected:
        normalized.append(
            {
                "timestamp_ms": bar.timestamp_ms,
                "open_pct": pct(bar.open),
                "high_pct": pct(bar.high),
                "low_pct": pct(bar.low),
                "close_pct": pct(bar.close),
                "body_pct": _decimal_text(
                    (bar.close - bar.open) / base * Decimal("100"), 6
                ),
                "range_pct": _decimal_text(
                    (bar.high - bar.low) / base * Decimal("100"), 6
                ),
                "volume_ratio": _decimal_text(
                    bar.volume / average_volume
                    if average_volume > 0
                    else Decimal("0"),
                    6,
                ),
            }
        )

    close_change = (selected[-1].close / selected[0].close - Decimal("1")) * Decimal(
        "100"
    )
    total_range = (
        max(bar.high for bar in selected) - min(bar.low for bar in selected)
    ) / base * Decimal("100")
    last_volume_ratio = (
        selected[-1].volume / average_volume
        if average_volume > 0
        else Decimal("0")
    )
    trend = "UP" if close_change >= 1 else "DOWN" if close_change <= -1 else "FLAT"
    volatility = "HIGH" if total_range >= 3 else "MEDIUM" if total_range >= 1 else "LOW"
    volume_state = (
        "EXPANDING"
        if last_volume_ratio >= Decimal("1.5")
        else "SHRINKING"
        if last_volume_ratio <= Decimal("0.67")
        else "NORMAL"
    )
    return {
        "available": True,
        "bar_count": len(selected),
        "interval": selected[-1].interval,
        "start_time_ms": selected[0].timestamp_ms,
        "end_time_ms": selected[-1].timestamp_ms,
        "close_change_pct": _decimal_text(close_change, 6),
        "total_range_pct": _decimal_text(total_range, 6),
        "last_volume_ratio": _decimal_text(last_volume_ratio, 6),
        "shape_signature": f"{trend}_{volatility}_{volume_state}",
        "normalized_bars": normalized,
    }


def write_experience_document(
    path: Path, experiences: Iterable[TradeExperience]
) -> Path:
    target = Path(path)
    items = list(experiences)
    payload = _experience_document(items)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)
    except OSError as exc:
        raise ExperienceError(f"写入经验文件失败：{exc}") from exc
    return target


def merge_experience_document(
    path: Path, experiences: Iterable[TradeExperience]
) -> tuple[Path, int, int]:
    target = Path(path)
    existing: dict[str, dict[str, Any]] = {}
    if target.exists():
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            rows = payload.get("experiences", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                raise ValueError("experiences不是列表")
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("experience_id"), str):
                    existing[row["experience_id"]] = row
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ExperienceError(f"现有经验库格式错误：{exc}") from exc
    previous = len(existing)
    for item in experiences:
        existing[item.experience_id] = asdict(item)
    merged = sorted(
        existing.values(),
        key=lambda row: (int(row.get("entry_time_ms", 0)), str(row.get("experience_id", ""))),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(merged),
        "experiences": merged,
    }
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)
    except OSError as exc:
        raise ExperienceError(f"写入本地经验库失败：{exc}") from exc
    return target, len(merged) - previous, len(merged)


class OpenAIVectorStoreUploader:
    def __init__(
        self,
        *,
        request_json: Callable[[str, dict[str, Any], str, int], dict[str, Any]]
        | None = None,
        upload_file: Callable[[Path, str, int], dict[str, Any]] | None = None,
    ) -> None:
        self._request_json = request_json or _post_json
        self._upload_file = upload_file or _upload_openai_file

    def upload(
        self,
        path: Path,
        *,
        api_key: str,
        vector_store_id: str = "",
        timeout_seconds: int = 30,
    ) -> UploadResult:
        if not api_key.strip():
            raise ExperienceError("缺少 OpenAI API Key")
        if not 5 <= timeout_seconds <= 120:
            raise ExperienceError("上传超时必须在 5 到 120 秒之间")
        store_id = vector_store_id.strip()
        if store_id and not _valid_resource_id(store_id, "vs_"):
            raise ExperienceError("Vector Store ID 格式不正确")
        if not store_id:
            store = self._request_json(
                OPENAI_VECTOR_STORES_URL,
                {"name": "AutoQuant 交易经验库"},
                api_key,
                timeout_seconds,
            )
            store_id = str(store.get("id", ""))
            if not _valid_resource_id(store_id, "vs_"):
                raise ExperienceError("OpenAI 未返回有效的 Vector Store ID")

        uploaded = self._upload_file(Path(path), api_key, timeout_seconds)
        file_id = str(uploaded.get("id", ""))
        if not _valid_resource_id(file_id, "file-"):
            raise ExperienceError("OpenAI 未返回有效的文件 ID")
        attached = self._request_json(
            f"{OPENAI_VECTOR_STORES_URL}/{store_id}/files",
            {"file_id": file_id},
            api_key,
            timeout_seconds,
        )
        return UploadResult(
            vector_store_id=store_id,
            file_id=file_id,
            attachment_id=str(attached.get("id", "")),
            status=str(attached.get("status", "in_progress")),
        )


def _experience_document(items: list[TradeExperience]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "experiences": [asdict(item) for item in items],
    }


def _upload_openai_file(
    path: Path, api_key: str, timeout_seconds: int
) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ExperienceError(f"无法读取待上传文件：{exc}") from exc
    boundary = f"----AutoQuant{uuid.uuid4().hex}"
    filename = path.name.encode("ascii", "ignore").decode("ascii") or "experiences.json"
    chunks = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="purpose"\r\n\r\n',
        b"assistants\r\n",
        f"--{boundary}\r\n".encode(),
        (
            'Content-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'
        ).encode(),
        b"Content-Type: application/json\r\n\r\n",
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    request = Request(
        OPENAI_FILES_URL,
        data=b"".join(chunks),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "User-Agent": "AutoQuant/0.4.0",
        },
        method="POST",
    )
    return _read_json_request(request, timeout_seconds)


def _post_json(
    url: str,
    payload: dict[str, Any],
    api_key: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AutoQuant/0.4.0",
        },
        method="POST",
    )
    return _read_json_request(request, timeout_seconds)


def _read_json_request(request: Request, timeout_seconds: int) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            content = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        message = f"OpenAI HTTP {exc.code}"
        try:
            body = exc.read(16_384)
            payload = json.loads(body.decode("utf-8"))
            error = payload.get("error") if isinstance(payload, dict) else None
            detail = error.get("message") if isinstance(error, dict) else None
            if isinstance(detail, str) and detail.strip():
                message += "：" + " ".join(detail.split())[:300]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise ExperienceError(message) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ExperienceError("OpenAI 上传网络请求失败或超时") from exc
    if len(content) > MAX_HTTP_RESPONSE_BYTES:
        raise ExperienceError("OpenAI 上传响应超过大小限制")
    try:
        result = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperienceError("OpenAI 返回了无法解析的上传响应") from exc
    if not isinstance(result, dict):
        raise ExperienceError("OpenAI 上传响应格式错误")
    return result


def _valid_resource_id(value: str, prefix: str) -> bool:
    if not value.startswith(prefix) or len(value) > 200:
        return False
    return all(character.isalnum() or character in "_-" for character in value)


def _parse_timestamp_ms(value: str) -> int:
    text = value.strip()
    if not text:
        raise ValueError("timestamp为空")
    try:
        numeric = Decimal(text)
        if not numeric.is_finite() or numeric <= 0:
            raise ValueError("timestamp必须为正数")
        result = int(numeric)
        if result < 100_000_000_000:
            result *= 1000
        return result
    except InvalidOperation:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp必须是毫秒、秒或ISO时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _parse_interval_ms(value: str) -> int:
    text = value.strip().lower()
    if len(text) < 2 or text[-1] not in {"s", "m", "h", "d"}:
        raise ValueError("interval必须类似1m、5m、1h或1d")
    try:
        amount = int(text[:-1])
    except ValueError as exc:
        raise ValueError("interval必须类似1m、5m、1h或1d") from exc
    if amount <= 0:
        raise ValueError("interval必须为正数")
    multiplier = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return amount * multiplier[text[-1]]


def _decimal_text(value: Decimal, places: int | None = None) -> str:
    normalized = value
    if places is not None:
        quantum = Decimal("1").scaleb(-places)
        normalized = value.quantize(quantum)
    return format(normalized, "f")


def _iso_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


def _clean_note(value: str) -> str:
    return " ".join(str(value).split())[:500]

from __future__ import annotations

import unittest
import tempfile
from decimal import Decimal
from pathlib import Path
from threading import Event

from autoquant_shared.config import AppConfig
from autoquant_backend.engine import (
    RunnerConfig,
    SymbolRunner,
    create_opening_decider,
    create_provider,
)
from autoquant_backend.ai_decision import EntryTimingDecision, OpeningDecision
from autoquant_shared.models import (
    Bar,
    Direction,
    OrderRequest,
    OrderResult,
    RunState,
    Side,
)
from autoquant_backend.state import OrderLedger


def make_bar(
    close: str,
    index: int,
    interval: str = "5m",
    open_price: str | None = None,
    symbol: str = "AAPL",
) -> Bar:
    price = Decimal(close)
    if interval == "1d":
        open_time = index * 86_400_000
        close_time = open_time + 86_400_000 - 1
    else:
        open_time = index * 300_000
        close_time = (index + 1) * 300_000 - 1
    return Bar(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=close_time,
        open=Decimal(open_price or close),
        high=price + Decimal("0.5"),
        low=price - Decimal("0.5"),
        close=price,
        closed=True,
    )


class FakeProvider:
    def __init__(self) -> None:
        self.orders = []

    def check_symbol(self, symbol: str) -> dict:
        return {"symbol": symbol, "tradability": "BUY_SELL"}

    def get_historical_bars(
        self, symbol, interval, start_time, end_time, limit
    ):
        if interval == "1d":
            return [
                make_bar("99", -2, interval="1d"),
                make_bar("100", -1, interval="1d"),
            ][-limit:]
        return []

    def stream_bars(self, symbol: str, stop_event: Event, status_callback=None):
        yield make_bar("101", 0, interval="1d", open_price="100")
        for index in range(3):
            yield make_bar("10", index)
        yield make_bar("12", 3)

    def place_order(self, order):
        self.orders.append(order)
        return OrderResult(True, "paper-test", "ok", True)


class ShortSignalProvider(FakeProvider):
    supports_short = False

    def stream_bars(self, symbol: str, stop_event: Event, status_callback=None):
        yield make_bar("99", 0, interval="1d", open_price="100")
        for index in range(3):
            yield make_bar("10", index)
        yield make_bar("8", 3)

    def get_historical_bars(
        self, symbol, interval, start_time, end_time, limit
    ):
        if interval == "1d":
            return [
                make_bar("101", -2, interval="1d"),
                make_bar("100", -1, interval="1d"),
            ][-limit:]
        return []


class FuturesShortSignalProvider(ShortSignalProvider):
    supports_short = True


class MissingDailyHistoryProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.history_requests = []

    def get_historical_bars(
        self, symbol, interval, start_time, end_time, limit
    ):
        self.history_requests.append(
            (symbol, interval, start_time, end_time, limit)
        )
        return []


class HistoricalWarmupProvider(FakeProvider):
    def __init__(self, history: list[Bar], live: list[Bar] | None = None) -> None:
        super().__init__()
        self.history = history
        self.live = live or []
        self.history_requests = []

    def get_historical_bars(
        self, symbol, interval, start_time, end_time, limit
    ):
        if interval == "1d":
            return super().get_historical_bars(
                symbol, interval, start_time, end_time, limit
            )
        self.history_requests.append(
            (symbol, interval, start_time, end_time, limit)
        )
        return self.history[-limit:]

    def stream_bars(self, symbol: str, stop_event: Event, status_callback=None):
        yield make_bar("101", 0, interval="1d", open_price="100")
        yield from self.live


class UnknownResultProvider(FakeProvider):
    def place_order(self, order):
        self.orders.append(order)
        raise RuntimeError("connection timed out")


class StopDuringValidationProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.checks = 0
        self.stop_callback = lambda: None

    def check_symbol(self, symbol: str) -> dict:
        self.checks += 1
        if self.checks == 2:
            self.stop_callback()
        return super().check_symbol(symbol)


class FilledLiveProvider(FakeProvider):
    def place_order(self, order):
        self.orders.append(order)
        return OrderResult(True, "live-filled", "accepted", False)

    def get_order_detail(self, order_id: str, symbol: str = "") -> dict:
        return {
            "status": "FILLED",
            "filledQty": "0.5",
            "avgFilledPrice": "200",
            "fee": "0.25",
        }


class MalformedFillProvider(FilledLiveProvider):
    def get_order_detail(self, order_id: str, symbol: str = "") -> dict:
        return {"status": "FILLED"}


class WaitingPaperProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()

    def stream_bars(self, symbol: str, stop_event: Event, status_callback=None):
        self.started.set()
        stop_event.wait(2)
        if False:
            yield make_bar("100", 0)


class WaitingFuturesPaperProvider(WaitingPaperProvider):
    supports_short = True


class WaitingFilledLiveProvider(FilledLiveProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.detail_calls = 0

    def stream_bars(self, symbol: str, stop_event: Event, status_callback=None):
        self.started.set()
        stop_event.wait(2)
        if False:
            yield make_bar("100", 0)

    def get_order_detail(self, order_id: str, symbol: str = "") -> dict:
        self.detail_calls += 1
        if self.detail_calls == 1:
            return {"status": "NEW", "filledQty": "0"}
        return super().get_order_detail(order_id, symbol)


class FlatOpeningDecider:
    def __init__(self) -> None:
        self.calls = []

    def decide(self, symbol, current_daily_bar):
        self.calls.append((symbol, current_daily_bar.open_time))
        return OpeningDecision(
            direction=Direction.FLAT,
            confidence=0.88,
            summary="新闻与走势相互冲突",
            provider="CHATGPT",
            model="gpt-test",
        )


class TracedOpeningDecider:
    def __init__(self) -> None:
        self.input_capture = lambda *_args: None
        self.output_capture = lambda *_args: None

    def decide(self, symbol, current_daily_bar):
        self.input_capture(
            "OPENING_DIRECTION",
            "CHATGPT",
            "gpt-test",
            {"symbol": symbol, "close": str(current_daily_bar.close)},
        )
        self.output_capture(
            "OPENING_DIRECTION",
            "CHATGPT",
            "gpt-test",
            {"output": [{"direction": "FLAT"}]},
        )
        return OpeningDecision(
            direction=Direction.FLAT,
            confidence=0.75,
            summary="输入与输出已捕获",
            factors=("测试依据",),
            risks=("测试风险",),
            provider="CHATGPT",
            model="gpt-test",
        )


class EntryGateDecider:
    def __init__(self, enter_now: bool) -> None:
        self.enter_now = enter_now
        self.direction_calls = []
        self.entry_calls = []

    def decide(self, symbol, current_daily_bar):
        self.direction_calls.append((symbol, current_daily_bar.open_time))
        return OpeningDecision(
            direction=Direction.LONG,
            confidence=0.88,
            summary="今日偏多",
            provider="CHATGPT",
            model="gpt-test",
        )

    def decide_entry(self, symbol, signal, current_bar, recent_bars=()):
        self.entry_calls.append(
            (symbol, signal.bar_open_time, current_bar.open_time, len(recent_bars))
        )
        return EntryTimingDecision(
            enter_now=self.enter_now,
            confidence=0.86,
            summary="允许入场" if self.enter_now else "等待下一个信号",
            provider="CHATGPT",
            model="gpt-test",
        )


class SymbolRunnerTests(unittest.TestCase):
    def test_qwen_mode_builds_qwen_decision_client(self) -> None:
        decider = create_opening_decider(
            RunnerConfig(
                AppConfig(
                    symbols=["AAPL"],
                    ai_provider="QWEN",
                    qwen_thinking_enabled=True,
                    qwen_reasoning_effort="high",
                ),
                qwen_api_key="qwen-secret",
            )
        )

        self.assertIsNotNone(decider)
        self.assertEqual("QWEN", decider.clients[0].provider)  # type: ignore[union-attr]
        self.assertTrue(decider.clients[0].thinking_enabled)  # type: ignore[union-attr]
        self.assertEqual("high", decider.clients[0].reasoning_effort)  # type: ignore[union-attr]

    def test_ai_decision_persists_final_result_input_and_raw_output(self) -> None:
        decider = TracedOpeningDecider()
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        ma_period=3,
                        ai_provider="CHATGPT",
                    ),
                    openai_api_key="test-key",
                    manual_direction=Direction.UNKNOWN,
                ),
                lambda _snapshot: None,
                lambda *_args: None,
                ledger,
                opening_decider=decider,
            )
            decider.input_capture = runner._capture_ai_input
            decider.output_capture = runner._capture_ai_output
            runner.provider = FakeProvider()

            runner.start()
            runner.join(timeout=2)
            records = ledger.ai_decision_history(
                symbol="AAPL",
                stage="OPENING_DIRECTION",
            )

        self.assertEqual(1, len(records))
        self.assertEqual("FLAT", records[0].outcome)
        self.assertEqual(0.75, records[0].confidence)
        self.assertIn('"symbol":"AAPL"', records[0].input_json)
        self.assertIn('"direction":"FLAT"', records[0].output_json)

    def test_manual_mode_subscribes_only_to_five_minute_stream(self) -> None:
        provider = create_provider(
            RunnerConfig(
                AppConfig(symbols=["AAPL"]),
                manual_direction=Direction.FLAT,
            )
        )

        self.assertFalse(provider.include_daily_stream)

    def test_futures_provider_receives_configured_leverage(self) -> None:
        provider = create_provider(
            RunnerConfig(
                AppConfig(
                    symbols=["BTCUSDT"],
                    provider="binance_futures",
                    leverage=8,
                ),
                manual_direction=Direction.FLAT,
            )
        )

        self.assertEqual("binance_futures", provider.name)
        self.assertEqual(8, provider.leverage)
        self.assertFalse(provider.include_daily_stream)

    def test_manual_direction_is_used_when_daily_history_is_missing(self) -> None:
        snapshots = []
        logs = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], ma_period=3),
                    manual_direction=Direction.LONG,
                ),
                snapshots.append,
                lambda level, symbol, message: logs.append(
                    (level, symbol, message)
                ),
                ledger,
            )
            provider = MissingDailyHistoryProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual(1, len(provider.orders))
            self.assertTrue(
                any(snapshot.direction is Direction.LONG for snapshot in snapshots)
            )
            self.assertTrue(
                any(
                    "实时 K 线" in snapshot.message
                    for snapshot in snapshots
                )
            )
            self.assertEqual([], provider.history_requests)

    def test_manual_direction_is_visible_before_first_daily_bar(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], ma_period=3),
                    manual_direction=Direction.SHORT,
                ),
                snapshots.append,
                lambda *_args: None,
                OrderLedger(Path(directory) / "orders.sqlite3"),
            )

            runner._refresh_market_snapshot()

            self.assertIs(Direction.SHORT, snapshots[-1].direction)
            self.assertIn("手动方向 SHORT 已设置", snapshots[-1].message)
            self.assertNotIn("等待日线方向", snapshots[-1].message)

    def test_missing_daily_history_without_manual_direction_stays_unknown(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(AppConfig(symbols=["AAPL"], ma_period=3)),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            provider = MissingDailyHistoryProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual([], provider.orders)
            self.assertFalse(
                any(snapshot.direction is Direction.LONG for snapshot in snapshots)
            )

    def test_manual_direction_replaces_daily_direction(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], ma_period=3),
                    manual_direction=Direction.SHORT,
                ),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            provider = FakeProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual([], provider.orders)
            self.assertTrue(
                any(snapshot.direction is Direction.SHORT for snapshot in snapshots)
            )

    def test_manual_mode_does_not_load_historical_warmup_bars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], ma_period=3),
                    manual_direction=Direction.LONG,
                ),
                lambda _snapshot: None,
                lambda *_args: None,
                ledger,
            )
            provider = HistoricalWarmupProvider(
                [make_bar("10", index) for index in range(4)],
                live=[make_bar("12", 4)],
            )
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual([], provider.history_requests)
            self.assertEqual([], provider.orders)

    def test_futures_preloads_six_closed_bars_before_realtime(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "BTCUSDT",
                RunnerConfig(
                    AppConfig(
                        symbols=["BTCUSDT"],
                        provider="binance_futures",
                        ma_period=5,
                    ),
                    manual_direction=Direction.LONG,
                ),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            history = [
                make_bar("10", index, symbol="BTCUSDT")
                for index in range(5)
            ]
            history.append(make_bar("12", 5, symbol="BTCUSDT"))
            provider = HistoricalWarmupProvider(history)
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual(1, len(provider.history_requests))
            _symbol, interval, start_time, end_time, limit = (
                provider.history_requests[0]
            )
            self.assertEqual("5m", interval)
            self.assertEqual(6, limit)
            self.assertEqual(6 * 300_000 - 1, end_time - start_time)
            self.assertEqual([], provider.orders)
            self.assertEqual(6, snapshots[-1].warmup_bars)
            self.assertEqual(6, snapshots[-1].warmup_required)

    def test_signal_found_only_in_history_is_not_submitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(AppConfig(symbols=["AAPL"], ma_period=3)),
                lambda _snapshot: None,
                lambda *_args: None,
                ledger,
            )
            provider = HistoricalWarmupProvider(
                [
                    make_bar("10", 0),
                    make_bar("10", 1),
                    make_bar("10", 2),
                    make_bar("12", 3),
                ]
            )
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual([], provider.orders)

    def test_stop_force_closes_paper_position(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.BUY,
                    reference_price=Decimal("100"),
                    buy_notional=Decimal("50"),
                    sell_quantity=Decimal("1"),
                    client_order_id="aq-paper-buy",
                ),
                0,
                paper=True,
            )
            ledger.mark_lifecycle(
                "aq-paper-buy",
                "FILLED",
                filled_quantity=Decimal("0.5"),
                average_price=Decimal("100"),
            )
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(AppConfig(symbols=["AAPL"])),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            provider = WaitingPaperProvider()
            runner.provider = provider

            runner.start()
            self.assertTrue(provider.started.wait(1))
            runner.stop(close_position=True)
            runner.join(timeout=2)

            self.assertFalse(runner.is_alive)
            self.assertEqual(1, len(provider.orders))
            self.assertIs(Side.SELL, provider.orders[0].side)
            self.assertEqual(Decimal("0.5"), provider.orders[0].sell_quantity)
            self.assertEqual(
                Decimal("0"),
                ledger.position_summary("AAPL", paper=True).quantity,
            )
            self.assertEqual(RunState.STOPPED, snapshots[-1].state)

    def test_stop_force_closes_real_position(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.BUY,
                    reference_price=Decimal("100"),
                    buy_notional=Decimal("50"),
                    sell_quantity=Decimal("1"),
                    client_order_id="aq-live-buy",
                ),
                0,
                paper=False,
            )
            ledger.mark_lifecycle(
                "aq-live-buy",
                "FILLED",
                filled_quantity=Decimal("0.5"),
                average_price=Decimal("100"),
            )
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], trading_mode="REAL"),
                    api_key="key",
                    api_secret="secret",
                ),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            provider = WaitingFilledLiveProvider()
            runner.provider = provider

            runner.start()
            self.assertTrue(provider.started.wait(1))
            runner.stop(close_position=True)
            runner.join(timeout=2)

            self.assertFalse(runner.is_alive)
            self.assertEqual(1, len(provider.orders))
            self.assertEqual(2, provider.detail_calls)
            self.assertIs(Side.SELL, provider.orders[0].side)
            self.assertEqual(Decimal("0.5"), provider.orders[0].sell_quantity)
            self.assertEqual(
                Decimal("0"),
                ledger.position_summary("AAPL", paper=False).quantity,
            )
            self.assertEqual(RunState.STOPPED, snapshots[-1].state)

    def test_stop_does_not_submit_close_while_order_is_unknown(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.BUY,
                    reference_price=Decimal("100"),
                    buy_notional=Decimal("50"),
                    sell_quantity=Decimal("1"),
                    client_order_id="aq-paper-buy",
                ),
                0,
                paper=True,
            )
            ledger.mark_lifecycle(
                "aq-paper-buy",
                "FILLED",
                filled_quantity=Decimal("0.5"),
                average_price=Decimal("100"),
            )
            ledger.record_submitting(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.SELL,
                    reference_price=Decimal("100"),
                    buy_notional=Decimal("0"),
                    sell_quantity=Decimal("0.5"),
                    client_order_id="aq-unknown-sell",
                    reduce_only=True,
                ),
                0,
                paper=True,
            )
            ledger.mark_unknown("aq-unknown-sell", "timeout")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(AppConfig(symbols=["AAPL"])),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            provider = WaitingPaperProvider()
            runner.provider = provider

            runner.stop(close_position=True)
            runner.join(timeout=2)

            self.assertEqual([], provider.orders)
            self.assertEqual(
                Decimal("0.5"),
                ledger.position_summary("AAPL", paper=True).quantity,
            )
            self.assertEqual(RunState.ERROR, snapshots[-1].state)

    def test_runner_turns_strategy_signal_into_paper_order(self) -> None:
        snapshots = []
        logs = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        ma_period=3,
                        buy_notional="100",
                        sell_quantity="1",
                    ),
                    manual_direction=Direction.LONG,
                ),
                snapshots.append,
                lambda level, symbol, message: logs.append((level, symbol, message)),
                ledger,
            )
            fake_provider = FakeProvider()
            runner.provider = fake_provider

            runner.start()
            runner.join(timeout=2)

            self.assertFalse(runner.is_alive)
            self.assertEqual(1, len(fake_provider.orders))
            order_messages = [
                message
                for level, _symbol, message in logs
                if level == "ORDER"
            ]
            self.assertTrue(order_messages)
            self.assertIn(
                "开仓成交｜标的 AAPL｜交易模式 模拟｜开仓方向 多头｜价格 12.00",
                order_messages[-1],
            )
            self.assertIn("｜金额 100.00｜收益 0.00", order_messages[-1])
            self.assertNotIn("paper-test", order_messages[-1])
            self.assertEqual(1, max(snapshot.trades_today for snapshot in snapshots))
            self.assertEqual(
                Decimal("100"),
                max(snapshot.session_open_notional for snapshot in snapshots),
            )
            self.assertEqual(
                Decimal("0"), snapshots[-1].session_open_notional
            )
            signal_messages = [
                message
                for level, _symbol, message in logs
                if level == "SIGNAL"
            ]
            self.assertTrue(signal_messages)
            self.assertIn(
                "开仓信号｜标的 AAPL｜交易模式 模拟｜开仓方向 多头｜"
                "价格 12.00｜MA 10.67｜原因 ",
                signal_messages[-1],
            )

    def test_close_log_contains_trade_values_and_net_profit_without_order_id(self) -> None:
        logs = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            opening = OrderRequest(
                symbol="AAPL",
                side=Side.BUY,
                reference_price=Decimal("100"),
                buy_notional=Decimal("200"),
                sell_quantity=Decimal("2"),
                client_order_id="aq-opening-secret",
            )
            ledger.record_submitting(opening, 0, paper=True)
            ledger.mark_lifecycle(
                opening.client_order_id,
                "FILLED",
                filled_quantity=Decimal("2"),
                average_price=Decimal("100"),
                fee=Decimal("1"),
            )
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(AppConfig(symbols=["AAPL"])),
                lambda _snapshot: None,
                lambda level, symbol, message: logs.append(
                    (level, symbol, message)
                ),
                ledger,
            )
            runner.provider = FakeProvider()
            closing = OrderRequest(
                symbol="AAPL",
                side=Side.SELL,
                reference_price=Decimal("110"),
                buy_notional=Decimal("0"),
                sell_quantity=Decimal("2"),
                client_order_id="aq-closing-secret",
                reduce_only=True,
            )
            ledger.record_submitting(closing, 0, paper=True)

            runner._submit_order(closing, is_paper=True)
            persisted_closes = ledger.trade_history(
                symbol="AAPL",
                action="CLOSE",
                paper=True,
            )

        order_messages = [
            message for level, _symbol, message in logs if level == "ORDER"
        ]
        self.assertEqual(
            "平仓成交｜标的 AAPL｜交易模式 模拟｜开仓方向 多头｜"
            "价格 110.00｜数量 2｜"
            "金额 220.00｜收益 19.00",
            order_messages[-1],
        )
        self.assertNotIn("aq-closing-secret", str(logs))
        self.assertNotIn("paper-test", str(logs))
        self.assertEqual(Decimal("19"), persisted_closes[0].profit)

    def test_ai_flat_direction_blocks_an_otherwise_valid_entry(self) -> None:
        logs = []
        decider = FlatOpeningDecider()
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        ma_period=3,
                        ai_provider="CHATGPT",
                    ),
                    openai_api_key="test-key",
                    manual_direction=Direction.UNKNOWN,
                ),
                lambda _snapshot: None,
                lambda level, symbol, message: logs.append(
                    (level, symbol, message)
                ),
                ledger,
                opening_decider=decider,
            )
            provider = FakeProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual([], provider.orders)
            self.assertEqual([("AAPL", 0)], decider.calls)
            self.assertTrue(any("今日方向=FLAT" in item[2] for item in logs))
            self.assertTrue(any("决策耗时=" in item[2] for item in logs))

    def test_ai_entry_timing_wait_blocks_candidate_order(self) -> None:
        logs = []
        decider = EntryGateDecider(enter_now=False)
        with tempfile.TemporaryDirectory() as directory:
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        ma_period=3,
                        ai_provider="CHATGPT",
                    ),
                    openai_api_key="test-key",
                    manual_direction=Direction.UNKNOWN,
                ),
                lambda _snapshot: None,
                lambda level, symbol, message: logs.append(
                    (level, symbol, message)
                ),
                OrderLedger(Path(directory) / "orders.sqlite3"),
                opening_decider=decider,
            )
            provider = FakeProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual([], provider.orders)
            self.assertEqual(1, len(decider.entry_calls))
            self.assertTrue(
                any("开仓时机=WAIT" in message for _, _, message in logs)
            )
            self.assertTrue(
                any("决策耗时=" in message for _, _, message in logs)
            )

    def test_ai_entry_timing_enter_allows_candidate_order(self) -> None:
        decider = EntryGateDecider(enter_now=True)
        with tempfile.TemporaryDirectory() as directory:
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        ma_period=3,
                        ai_provider="CHATGPT",
                    ),
                    openai_api_key="test-key",
                    manual_direction=Direction.UNKNOWN,
                ),
                lambda _snapshot: None,
                lambda *_args: None,
                OrderLedger(Path(directory) / "orders.sqlite3"),
                opening_decider=decider,
            )
            provider = FakeProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual(1, len(provider.orders))
            self.assertEqual(1, len(decider.entry_calls))

    def test_entry_order_uses_configured_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        ma_period=3,
                        buy_notional="75",
                        sell_quantity="0.5",
                        max_order_notional="150",
                    ),
                    manual_direction=Direction.LONG,
                ),
                lambda _snapshot: None,
                lambda *_args: None,
                ledger,
            )
            provider = FakeProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual(1, len(provider.orders))
            self.assertEqual(Decimal("75"), provider.orders[0].buy_notional)
            self.assertEqual(Decimal("0.5"), provider.orders[0].sell_quantity)

    def test_unknown_submission_is_persisted_and_stops_runner(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], ma_period=3),
                    manual_direction=Direction.LONG,
                ),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            provider = UnknownResultProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual(1, ledger.unknown_count("AAPL"))
            self.assertEqual(1, ledger.count_consumed("AAPL", 0))
            self.assertEqual("ERROR", snapshots[-1].state.value)

    def test_real_short_signal_is_blocked_before_order_submission(self) -> None:
        logs = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        trading_mode="REAL",
                        ma_period=3,
                    ),
                    api_key="key",
                    api_secret="secret",
                    manual_direction=Direction.SHORT,
                ),
                lambda _snapshot: None,
                lambda level, symbol, message: logs.append(
                    (level, symbol, message)
                ),
                ledger,
            )
            provider = ShortSignalProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual([], provider.orders)
            self.assertEqual(0, ledger.count_consumed("AAPL", 0))
            self.assertTrue(any("建立空头" in message for _, _, message in logs))

    def test_futures_short_signal_opens_single_short_position(self) -> None:
        logs = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        provider="binance_futures",
                        ma_period=3,
                        buy_notional="100",
                    ),
                    manual_direction=Direction.SHORT,
                ),
                lambda _snapshot: None,
                lambda level, symbol, message: logs.append(
                    (level, symbol, message)
                ),
                ledger,
            )
            provider = FuturesShortSignalProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual(1, len(provider.orders))
            submitted = provider.orders[0]
            self.assertIs(Side.SELL, submitted.side)
            self.assertFalse(submitted.reduce_only)
            self.assertTrue(submitted.allow_short)
            self.assertTrue(
                any(
                    "开仓成交｜标的 AAPL｜交易模式 模拟｜开仓方向 空头"
                    in message
                    for level, _symbol, message in logs
                    if level == "ORDER"
                )
            )
            self.assertTrue(
                any(
                    "开仓信号｜标的 AAPL｜交易模式 模拟｜开仓方向 空头｜"
                    "价格 8.00｜"
                    in message
                    for level, _symbol, message in logs
                    if level == "SIGNAL"
                )
            )
            self.assertLess(
                ledger.position_summary("AAPL", paper=True).quantity,
                Decimal("0"),
            )

    def test_stop_force_closes_futures_short_with_reduce_only_buy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.SELL,
                    reference_price=Decimal("100"),
                    buy_notional=Decimal("50"),
                    sell_quantity=Decimal("0"),
                    client_order_id="aq-paper-short",
                    allow_short=True,
                ),
                0,
                paper=True,
            )
            ledger.mark_lifecycle(
                "aq-paper-short",
                "FILLED",
                filled_quantity=Decimal("0.5"),
                average_price=Decimal("100"),
            )
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], provider="binance_futures")
                ),
                lambda _snapshot: None,
                lambda *_args: None,
                ledger,
            )
            provider = WaitingFuturesPaperProvider()
            runner.provider = provider

            runner.start()
            self.assertTrue(provider.started.wait(1))
            runner.stop(close_position=True)
            runner.join(timeout=2)

            self.assertEqual(1, len(provider.orders))
            submitted = provider.orders[0]
            self.assertIs(Side.BUY, submitted.side)
            self.assertTrue(submitted.reduce_only)
            self.assertEqual(Decimal("0.5"), submitted.sell_quantity)
            self.assertEqual(
                Decimal("0"),
                ledger.position_summary("AAPL", paper=True).quantity,
            )

    def test_short_stop_loss_and_take_profit_emit_buy_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        provider="binance_futures",
                        stop_loss_percent="2",
                        take_profit_percent="4",
                    )
                ),
                lambda _snapshot: None,
                lambda *_args: None,
                OrderLedger(Path(directory) / "orders.sqlite3"),
            )
            runner._position_quantity = Decimal("-1")
            runner._average_entry_price = Decimal("100")

            stop_signal = runner._risk_exit_signal(make_bar("102", 1))
            take_signal = runner._risk_exit_signal(make_bar("96", 2))

            self.assertIsNotNone(stop_signal)
            self.assertIs(Side.BUY, stop_signal.side)
            self.assertIn("空头", stop_signal.reason)
            self.assertEqual(
                "平仓信号｜标的 AAPL｜交易模式 模拟｜开仓方向 空头｜"
                "价格 102.00｜"
                "MA 102.00｜原因 风险止损：当前价 102.00 >= 止损价 102.00，"
                "平掉 1 股空头",
                runner._signal_message(
                    stop_signal,
                    is_exit=True,
                    position_quantity=Decimal("-1"),
                    paper=True,
                ),
            )
            self.assertIsNotNone(take_signal)
            self.assertIs(Side.BUY, take_signal.side)
            self.assertIn("空头", take_signal.reason)

    def test_stop_during_pre_order_validation_prevents_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        trading_mode="REAL",
                        ma_period=3,
                    ),
                    api_key="key",
                    api_secret="secret",
                    manual_direction=Direction.LONG,
                ),
                lambda _snapshot: None,
                lambda *_args: None,
                ledger,
            )
            provider = StopDuringValidationProvider()
            provider.stop_callback = runner.stop
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual([], provider.orders)
            self.assertEqual(0, ledger.count_consumed("AAPL", 0))

    def test_unknown_live_order_hard_locks_runner_on_restart(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            from autoquant_shared.models import OrderRequest, Side

            ledger.record_submitting(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.BUY,
                    reference_price=Decimal("100"),
                    buy_notional=Decimal("100"),
                    sell_quantity=Decimal("1"),
                    client_order_id="aq-unknown",
                ),
                0,
                paper=False,
            )
            ledger.mark_unknown("aq-unknown", "timeout")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], trading_mode="REAL", ma_period=3),
                    api_key="key",
                    api_secret="secret",
                    manual_direction=Direction.LONG,
                ),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            provider = FakeProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual("ERROR", snapshots[-1].state.value)
            self.assertEqual([], provider.orders)

    def test_real_sell_signal_closes_tracked_long_position(self) -> None:
        from autoquant_shared.models import OrderRequest, Side

        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            ledger.record_submitting(
                OrderRequest(
                    symbol="AAPL",
                    side=Side.BUY,
                    reference_price=Decimal("100"),
                    buy_notional=Decimal("50"),
                    sell_quantity=Decimal("1"),
                    client_order_id="aq-filled-buy",
                ),
                0,
                paper=False,
            )
            ledger.mark_lifecycle(
                "aq-filled-buy",
                "FILLED",
                filled_quantity=Decimal("0.5"),
                average_price=Decimal("100"),
            )
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(
                        symbols=["AAPL"],
                        trading_mode="REAL",
                        ma_period=3,
                        max_order_notional="500",
                        max_daily_buy_notional="500",
                    ),
                    api_key="key",
                    api_secret="secret",
                ),
                lambda _snapshot: None,
                lambda *_args: None,
                ledger,
            )
            provider = ShortSignalProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            self.assertEqual(1, len(provider.orders))
            self.assertEqual(Side.SELL, provider.orders[0].side)
            self.assertEqual(Decimal("0.5"), provider.orders[0].sell_quantity)

    def test_live_ack_is_reconciled_to_filled_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], trading_mode="REAL", ma_period=3),
                    api_key="key",
                    api_secret="secret",
                    manual_direction=Direction.LONG,
                ),
                lambda _snapshot: None,
                lambda *_args: None,
                ledger,
            )
            provider = FilledLiveProvider()
            runner.provider = provider

            runner.start()
            runner.join(timeout=2)

            position = ledger.position_summary("AAPL", paper=False)
            self.assertEqual(Decimal("0.5"), position.quantity)
            self.assertEqual(Decimal("200"), position.average_price)
            self.assertEqual(0, ledger.pending_count("AAPL", paper=False))
            record = ledger.get_record(provider.orders[0].client_order_id)
            self.assertIsNotNone(record)
            self.assertEqual(Decimal("0.25"), record.fee)

    def test_unverifiable_fill_hard_locks_live_runner(self) -> None:
        snapshots = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = OrderLedger(Path(directory) / "orders.sqlite3")
            runner = SymbolRunner(
                "AAPL",
                RunnerConfig(
                    AppConfig(symbols=["AAPL"], trading_mode="REAL", ma_period=3),
                    api_key="key",
                    api_secret="secret",
                    manual_direction=Direction.LONG,
                ),
                snapshots.append,
                lambda *_args: None,
                ledger,
            )
            runner.provider = MalformedFillProvider()

            runner.start()
            runner.join(timeout=2)

            self.assertEqual(1, ledger.unknown_count("AAPL", paper=False))
            self.assertEqual("ERROR", snapshots[-1].state.value)


if __name__ == "__main__":
    unittest.main()

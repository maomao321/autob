from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import format_datetime

from autoquant_backend.ai_decision import (
    DecisionError,
    DeepSeekDecisionClient,
    EntryTimingDecision,
    OpenAIResponsesDecisionClient,
    OpeningDecision,
    OpeningDecisionService,
    PublicMarketContextCollector,
    parse_entry_timing_decision,
    parse_opening_decision,
)
from autoquant_shared.models import Bar, Direction, Side, Signal


def daily_bar() -> Bar:
    return Bar(
        symbol="AAPL",
        interval="1d",
        open_time=1_000,
        close_time=2_000,
        open=Decimal("100"),
        high=Decimal("103"),
        low=Decimal("99"),
        close=Decimal("102"),
        closed=False,
    )


def decision_json(
    direction: str = "LONG", confidence: float = 0.82
) -> str:
    return json.dumps(
        {
            "direction": direction,
            "confidence": confidence,
            "summary": "大盘与个股趋势同向",
            "factors": ["SPY 近五日上涨", "个股站上均线"],
            "risks": ["新闻事件可能放大波动"],
        },
        ensure_ascii=False,
    )


def entry_timing_json(
    enter_now: bool = True, confidence: float = 0.84
) -> str:
    return json.dumps(
        {
            "enter_now": enter_now,
            "confidence": confidence,
            "summary": "突破与短线走势共振",
            "factors": ["收盘价突破前高", "均线方向一致"],
            "risks": ["短线波动可能放大"],
        },
        ensure_ascii=False,
    )


def candidate_signal() -> Signal:
    return Signal(
        symbol="AAPL",
        side=Side.BUY,
        price=Decimal("102"),
        ma_value=Decimal("101"),
        bar_open_time=1_500,
        reason="5 分钟突破",
    )


def intraday_bar() -> Bar:
    return Bar(
        symbol="AAPL",
        interval="5m",
        open_time=1_500,
        close_time=1_800,
        open=Decimal("101"),
        high=Decimal("103"),
        low=Decimal("100"),
        close=Decimal("102"),
        closed=True,
    )


def intraday_history(count: int = 60) -> tuple[Bar, ...]:
    bars = []
    for index in range(count):
        open_time = 1_500 - (count - index - 1) * 300_000
        price = Decimal("100") + Decimal(index) / Decimal("10")
        bars.append(
            Bar(
                symbol="AAPL",
                interval="5m",
                open_time=open_time,
                close_time=open_time + 299_999,
                open=price,
                high=price + Decimal("1"),
                low=price - Decimal("1"),
                close=price + Decimal("0.5"),
                closed=True,
            )
        )
    return tuple(bars)


class StaticCollector:
    def __init__(self) -> None:
        self.symbols = []

    def collect(self, symbol: str, current_daily_bar: Bar):
        self.symbols.append(symbol)
        return {
            "symbol": symbol,
            "current": str(current_daily_bar.close),
            "recent_news": [{"title": "test"}],
        }


class StaticClient:
    def __init__(
        self,
        provider: str,
        direction: Direction,
        confidence: float = 0.8,
        enter_now: bool = True,
    ) -> None:
        self.provider = provider
        self.model = provider.lower()
        self.direction = direction
        self.confidence = confidence
        self.enter_now = enter_now
        self.decision_contexts = []
        self.entry_contexts = []

    def decide(self, context):
        self.decision_contexts.append(context)
        return OpeningDecision(
            direction=self.direction,
            confidence=self.confidence,
            summary=f"{self.provider} conclusion",
            factors=("trend",),
            risks=("volatility",),
            provider=self.provider,
            model=self.model,
        )

    def decide_entry(self, context):
        self.entry_contexts.append(context)
        return EntryTimingDecision(
            enter_now=self.enter_now,
            confidence=self.confidence,
            summary=f"{self.provider} timing",
            factors=("breakout",),
            risks=("volatility",),
            provider=self.provider,
            model=self.model,
        )


class AiDecisionTests(unittest.TestCase):
    def test_public_context_combines_nasdaq_trends_and_news(self) -> None:
        rows = [
            {
                "date": f"08/{day:02d}/2026",
                "open": f"${99 + day}.00",
                "high": f"${101 + day}.00",
                "low": f"${98 + day}.00",
                "close": f"${100 + day}.00",
            }
            for day in range(1, 31)
        ]

        def get_bytes(url, _timeout):
            if "news.google.com" in url:
                published = format_datetime(
                    datetime.now(timezone.utc) - timedelta(days=1), usegmt=True
                )
                return (
                    "<rss><channel><item><title>AAPL launches product</title>"
                    "<link>https://example.com/story</link>"
                    "<source>Example</source>"
                    f"<pubDate>{published}</pubDate>"
                    "</item></channel></rss>"
                ).encode()
            return json.dumps(
                {"data": {"tradesTable": {"rows": rows}}}
            ).encode()

        collector = PublicMarketContextCollector(
            history_days=30,
            news_days=7,
            news_limit=3,
            timeout_seconds=10,
            get_bytes=get_bytes,
        )

        context = collector.collect("AAPL", daily_bar())

        self.assertEqual("AAPL", context["symbol"])
        self.assertEqual(30, context["symbol_trend"]["observations"])
        self.assertEqual("130.00", context["symbol_trend"]["latest_close"])
        self.assertEqual("128.00", context["symbol_trend"]["sma_5"])
        daily_bars = context["symbol_trend"]["daily_bars"]
        self.assertEqual(30, len(daily_bars))
        self.assertEqual(
            {"date", "open", "high", "low", "close"},
            set(daily_bars[-1]),
        )
        self.assertEqual("102.00", context["current_session"]["close"])
        self.assertEqual({"SPY", "QQQ"}, set(context["broad_market_trends"]))
        self.assertEqual("AAPL launches product", context["recent_news"][0]["title"])

    def test_parser_accepts_only_the_expected_contract(self) -> None:
        decision = parse_opening_decision(
            decision_json(), "CHATGPT", "gpt-test"
        )

        self.assertEqual(Direction.LONG, decision.direction)
        self.assertEqual(0.82, decision.confidence)
        self.assertEqual("CHATGPT", decision.provider)

        malformed = json.loads(decision_json())
        malformed["quantity"] = 100
        with self.assertRaisesRegex(DecisionError, "字段"):
            parse_opening_decision(
                json.dumps(malformed), "CHATGPT", "gpt-test"
            )

        timing = parse_entry_timing_decision(
            entry_timing_json(), "CHATGPT", "gpt-test"
        )
        self.assertTrue(timing.enter_now)
        malformed_timing = json.loads(entry_timing_json())
        malformed_timing["enter_now"] = "yes"
        with self.assertRaisesRegex(DecisionError, "enter_now"):
            parse_entry_timing_decision(
                json.dumps(malformed_timing), "CHATGPT", "gpt-test"
            )

    def test_openai_client_uses_structured_response_text(self) -> None:
        calls = []
        output_logs = []

        def post(url, payload, api_key, timeout):
            calls.append((url, payload, api_key, timeout))
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": decision_json()}
                        ],
                    }
                ]
            }

        client = OpenAIResponsesDecisionClient(
            "secret",
            "gpt-test",
            12,
            post_json=post,
            output_log_callback=output_logs.append,
        )
        decision = client.decide({"symbol": "AAPL"})

        self.assertEqual(Direction.LONG, decision.direction)
        self.assertEqual("json_schema", calls[0][1]["text"]["format"]["type"])
        self.assertFalse(calls[0][1]["store"])

        def timing_post(url, payload, api_key, timeout):
            calls.append((url, payload, api_key, timeout))
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": entry_timing_json(),
                            }
                        ],
                    }
                ]
            }

        timing_client = OpenAIResponsesDecisionClient(
            "secret",
            "gpt-test",
            12,
            post_json=timing_post,
            output_log_callback=output_logs.append,
        )
        timing = timing_client.decide_entry({"symbol": "AAPL"})
        self.assertTrue(timing.enter_now)
        self.assertEqual(
            "entry_timing", calls[-1][1]["text"]["format"]["name"]
        )
        self.assertEqual(2, len(output_logs))
        self.assertIn(
            "大模型今日方向原始输出（CHATGPT/gpt-test）", output_logs[0]
        )
        self.assertIn('"output"', output_logs[0])
        self.assertIn(
            "大模型开仓时机原始输出（CHATGPT/gpt-test）",
            output_logs[1],
        )

    def test_deepseek_retries_one_invalid_json_response(self) -> None:
        output_logs = []
        responses = [
            {
                "choices": [
                    {
                        "message": {
                            "content": decision_json("SHORT")[:-1] + "]}"
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": decision_json("SHORT")}}]},
        ]

        def post(_url, _payload, _api_key, _timeout):
            return responses.pop(0)

        client = DeepSeekDecisionClient(
            "secret",
            "deepseek-test",
            12,
            post_json=post,
            output_log_callback=output_logs.append,
        )
        decision = client.decide({"symbol": "AAPL"})

        self.assertEqual(Direction.SHORT, decision.direction)
        self.assertEqual([], responses)
        self.assertEqual(2, len(output_logs))
        self.assertIn("]]}", output_logs[0])
        self.assertIn('\\"direction\\": \\"SHORT\\"', output_logs[1])

    def test_deepseek_retries_one_invalid_entry_timing_response(self) -> None:
        responses = [
            {
                "choices": [
                    {
                        "message": {
                            "content": entry_timing_json()[:-1] + "]}"
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": entry_timing_json()}}]},
        ]

        def post(_url, _payload, _api_key, _timeout):
            return responses.pop(0)

        client = DeepSeekDecisionClient(
            "secret",
            "deepseek-test",
            12,
            post_json=post,
        )

        decision = client.decide_entry({"symbol": "AAPL"})

        self.assertTrue(decision.enter_now)
        self.assertEqual([], responses)

    def test_low_confidence_fails_closed(self) -> None:
        service = OpeningDecisionService(
            StaticCollector(),
            (StaticClient("CHATGPT", Direction.LONG, 0.6),),
            min_confidence=0.7,
            mode="CHATGPT",
        )

        decision = service.decide("AAPL", daily_bar())

        self.assertEqual(Direction.FLAT, decision.direction)
        self.assertTrue(decision.fallback)

    def test_logs_each_complete_model_input_once(self) -> None:
        client = StaticClient("CHATGPT", Direction.LONG, 0.82)
        input_logs = []
        service = OpeningDecisionService(
            StaticCollector(),
            (client,),
            min_confidence=0.7,
            mode="CHATGPT",
            input_log_callback=input_logs.append,
        )

        service.decide("AAPL", daily_bar())
        service.decide_entry(
            "AAPL", candidate_signal(), intraday_bar(), intraday_history()
        )

        self.assertEqual(2, len(input_logs))
        self.assertIn("大模型今日方向输入（CHATGPT/chatgpt）", input_logs[0])
        self.assertIn('"current_session"', input_logs[0])
        self.assertIn("大模型开仓时机输入（CHATGPT/chatgpt）", input_logs[1])
        self.assertIn('"candidate_entry"', input_logs[1])
        self.assertIn('"recent_intraday_bars"', input_logs[1])

    def test_tradfi_symbol_uses_underlying_for_public_market_data(self) -> None:
        collector = StaticCollector()
        client = StaticClient("CHATGPT", Direction.LONG, 0.82)
        service = OpeningDecisionService(
            collector,
            (client,),
            min_confidence=0.7,
            mode="CHATGPT",
        )
        service.set_market_data_symbol("SOXLUSDT", "SOXL")

        decision = service.decide("SOXLUSDT", daily_bar())

        self.assertEqual(Direction.LONG, decision.direction)
        self.assertEqual(["SOXL"], collector.symbols)
        self.assertEqual("SOXLUSDT", client.decision_contexts[-1]["symbol"])
        self.assertEqual("SOXL", client.decision_contexts[-1]["market_data_symbol"])

    def test_dual_mode_requires_same_direction(self) -> None:
        service = OpeningDecisionService(
            StaticCollector(),
            (
                StaticClient("CHATGPT", Direction.LONG),
                StaticClient("DEEPSEEK", Direction.SHORT),
            ),
            min_confidence=0.7,
            mode="DUAL",
        )

        decision = service.decide("AAPL", daily_bar())

        self.assertEqual(Direction.FLAT, decision.direction)
        self.assertTrue(decision.fallback)
        self.assertIn("未形成", decision.summary)

    def test_dual_mode_accepts_high_confidence_consensus(self) -> None:
        service = OpeningDecisionService(
            StaticCollector(),
            (
                StaticClient("CHATGPT", Direction.LONG, 0.81),
                StaticClient("DEEPSEEK", Direction.LONG, 0.77),
            ),
            min_confidence=0.7,
            mode="DUAL",
        )

        decision = service.decide("AAPL", daily_bar())

        self.assertEqual(Direction.LONG, decision.direction)
        self.assertEqual(0.77, decision.confidence)
        self.assertFalse(decision.fallback)

    def test_entry_timing_requires_confidence_and_dual_consensus(self) -> None:
        service = OpeningDecisionService(
            StaticCollector(),
            (
                StaticClient("CHATGPT", Direction.LONG, 0.81),
                StaticClient(
                    "DEEPSEEK",
                    Direction.LONG,
                    0.79,
                    enter_now=False,
                ),
            ),
            min_confidence=0.7,
            mode="DUAL",
        )
        service.decide("AAPL", daily_bar())

        decision = service.decide_entry(
            "AAPL",
            candidate_signal(),
            intraday_bar(),
            intraday_history(),
        )

        self.assertFalse(decision.enter_now)
        self.assertIn("未形成入场共识", decision.summary)

    def test_entry_timing_accepts_high_confidence_single_model(self) -> None:
        client = StaticClient("CHATGPT", Direction.LONG, 0.82)
        service = OpeningDecisionService(
            StaticCollector(),
            (client,),
            min_confidence=0.7,
            mode="CHATGPT",
        )
        service.decide("AAPL", daily_bar())

        decision = service.decide_entry(
            "AAPL", candidate_signal(), intraday_bar(), intraday_history()
        )

        self.assertTrue(decision.enter_now)
        self.assertEqual(0.82, decision.confidence)
        context = client.entry_contexts[-1]
        self.assertEqual(60, len(context["recent_intraday_bars"]))
        self.assertEqual(
            {"open_time_ms", "close_time_ms", "open", "high", "low", "close", "is_closed"},
            set(context["recent_intraday_bars"][-1]),
        )
        self.assertEqual("102.00", context["today_daily_bar"]["close"])

    def test_entry_timing_waits_before_calling_model_with_fewer_than_60_bars(self) -> None:
        client = StaticClient("CHATGPT", Direction.LONG, 0.82)
        service = OpeningDecisionService(
            StaticCollector(),
            (client,),
            min_confidence=0.7,
            mode="CHATGPT",
        )
        service.decide("AAPL", daily_bar())

        decision = service.decide_entry(
            "AAPL",
            candidate_signal(),
            intraday_bar(),
            intraday_history(59),
        )

        self.assertFalse(decision.enter_now)
        self.assertIn("不足 60 根", decision.summary)
        self.assertEqual([], client.entry_contexts)

    def test_entry_timing_bar_count_is_configurable(self) -> None:
        client = StaticClient("CHATGPT", Direction.LONG, 0.82)
        service = OpeningDecisionService(
            StaticCollector(),
            (client,),
            min_confidence=0.7,
            mode="CHATGPT",
            entry_timing_bar_count=20,
        )
        service.decide("AAPL", daily_bar())

        decision = service.decide_entry(
            "AAPL",
            candidate_signal(),
            intraday_bar(),
            intraday_history(20),
        )

        self.assertTrue(decision.enter_now)
        context = client.entry_contexts[-1]
        self.assertEqual(20, context["entry_timing_bar_count"])
        self.assertEqual(20, len(context["recent_intraday_bars"]))


if __name__ == "__main__":
    unittest.main()

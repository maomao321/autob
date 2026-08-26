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


class StaticCollector:
    def collect(self, symbol: str, current_daily_bar: Bar):
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

    def decide(self, context):
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
            {"date": f"08/{day:02d}/2026", "close": f"${100 + day}.00"}
            for day in range(1, 8)
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
        self.assertEqual(7, context["symbol_trend"]["observations"])
        self.assertEqual("107.00", context["symbol_trend"]["latest_close"])
        self.assertEqual("105.00", context["symbol_trend"]["sma_5"])
        self.assertEqual("102.00", context["current_session"]["current_close"])
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
            "secret", "gpt-test", 12, post_json=post
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
            "secret", "gpt-test", 12, post_json=timing_post
        )
        timing = timing_client.decide_entry({"symbol": "AAPL"})
        self.assertTrue(timing.enter_now)
        self.assertEqual(
            "entry_timing", calls[-1][1]["text"]["format"]["name"]
        )

    def test_deepseek_retries_one_empty_json_response(self) -> None:
        responses = [
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": decision_json("SHORT")}}]},
        ]

        def post(_url, _payload, _api_key, _timeout):
            return responses.pop(0)

        client = DeepSeekDecisionClient(
            "secret", "deepseek-test", 12, post_json=post
        )
        decision = client.decide({"symbol": "AAPL"})

        self.assertEqual(Direction.SHORT, decision.direction)
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
            (intraday_bar(),),
        )

        self.assertFalse(decision.enter_now)
        self.assertIn("未形成入场共识", decision.summary)

    def test_entry_timing_accepts_high_confidence_single_model(self) -> None:
        service = OpeningDecisionService(
            StaticCollector(),
            (StaticClient("CHATGPT", Direction.LONG, 0.82),),
            min_confidence=0.7,
            mode="CHATGPT",
        )
        service.decide("AAPL", daily_bar())

        decision = service.decide_entry(
            "AAPL", candidate_signal(), intraday_bar()
        )

        self.assertTrue(decision.enter_now)
        self.assertEqual(0.82, decision.confidence)


if __name__ == "__main__":
    unittest.main()

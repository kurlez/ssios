"""Creative and hypothetical sentences stay in chat. Real calendar and mail requests do not."""

from __future__ import annotations

import unittest

from ssios.chat import _style_instruction
from ssios.chat_router import classify
from ssios.orchestrator import choose_route
from ssios.route_classifier import decision_from_classifier
from ssios.router_bench import score_model, score_rules
from ssios.router_cases import CASES


class RouterNearMissTest(unittest.TestCase):
    def test_rules_match_the_synthetic_set(self) -> None:
        self.assertGreaterEqual(len(CASES), 40)
        prompts = {case["prompt"] for case in CASES}
        self.assertIn("帮我写一个小故事，描述一次积极开会但是却没有结果的经历。", prompts)
        self.assertIn("帮我写一个小故事", prompts)
        for case in CASES:
            decision = classify(case["prompt"])
            self.assertEqual(decision.route, case["route"], case["id"])
            if case["kind"] == "weather":
                self.assertEqual(decision.skill, "weather", case["id"])
            elif case["kind"]:
                self.assertIn(case["kind"], decision.services, case["id"])
            else:
                self.assertEqual(decision.services, (), case["id"])

    def test_a_story_stays_out_of_the_guest_even_when_the_guest_is_ready(self) -> None:
        for prompt in ("帮我写一个小故事", "帮我写一个小故事，描述一次积极开会但是却没有结果的经历。"):
            decision, note = choose_route(prompt, guest_ready=True)
            self.assertEqual(decision.route, "direct", prompt)
            self.assertNotEqual(decision.route, "codex", prompt)
            self.assertEqual(note, "")
            self.assertIn("Write it now", _style_instruction(prompt))
            self.assertIn("Do not ask for a theme", _style_instruction(prompt))
            self.assertIn("300 to 600", _style_instruction(prompt))

    def test_a_low_confidence_classifier_stays_on_chat(self) -> None:
        chosen = decision_from_classifier({"intent": "calendar", "tools": ["calendar"], "confidence": 0.2})
        self.assertEqual(chosen.route, "direct")
        story = decision_from_classifier({"intent": "creative", "tools": [], "confidence": 0.95})
        self.assertEqual(story.route, "direct")
        tools = decision_from_classifier({"intent": "codex", "tools": ["files"], "confidence": 0.9})
        self.assertEqual(tools.route, "vm")

    def test_rules_bench_is_exact_and_a_model_timeout_falls_back_to_chat(self) -> None:
        rules = score_rules()
        self.assertGreaterEqual(rules["count"], 40)
        self.assertEqual(rules["accuracy"], 1)
        self.assertIn("direct", rules["confusion"])
        self.assertIn("median", rules["latency_ms"])
        self.assertIn("max", rules["latency_ms"])

        def opener(url, payload, timeout):
            del url, payload, timeout
            raise TimeoutError("slow")

        scored = score_model("qwen3:1.7b", opener=opener)
        self.assertEqual(scored["mode"], "qwen3:1.7b")
        self.assertTrue(all(row["predicted"] == "direct" for row in scored["rows"]))

        def answer(url, payload, timeout):
            del url, timeout
            prompt = payload["messages"][-1]["content"]
            intent = "creative" if "故事" in prompt or "story" in prompt else "calendar"
            body = (
                '{"intent":"%s","tools":%s,"confidence":0.9}'
                % (intent, "[]" if intent == "creative" else '["calendar"]')
            ).encode()
            return 200, b'{"message":{"content":' + __import__("json").dumps(body.decode()).encode() + b"}}"

        mixed = score_model("qwen3:4b", opener=answer)
        story = next(row for row in mixed["rows"] if row["id"] == "story-short")
        self.assertTrue(story["ok"])
        week = next(row for row in mixed["rows"] if row["id"] == "cal-week")
        self.assertEqual(week["predicted"], "connector")

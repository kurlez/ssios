"""Mail and calendar reports use the requested count and only the fetched items."""

from __future__ import annotations

import unittest

from ssios.mail_intent import plan_mail
from ssios.read_report import compose_report, output_intent


class _Script:
    provider = "script"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    def complete_chat(self, messages, *, cancel_event, deadline: float) -> str:
        del messages, cancel_event, deadline
        if not self.replies:
            return ""
        return self.replies.pop(0)


def _items(count: int = 2) -> list[dict]:
    rows = []
    for index in range(count):
        rows.append(
            {
                "sender": "Ada North",
                "subject": "Harbor Desk",
                "date": "Nov 10",
                "body": "The studio plan is on the desk. " * 40,
            }
        )
    return rows


class ReportIntentTest(unittest.TestCase):
    def test_count_and_report_intent(self) -> None:
        phrase = "读取我的gmail最近的20封邮件，总结一下其中的内容，做成一个1000字的汇报给我。"
        intent = output_intent(phrase)
        self.assertEqual(intent["count"], 20)
        self.assertEqual(intent["mode"], "report")
        self.assertEqual(intent["language"], "zh")
        self.assertEqual(intent["target"], {"unit": "chars", "n": 1000})
        self.assertFalse(intent["capped"])
        self.assertEqual(output_intent("最近二十封邮件")["count"], 20)
        self.assertEqual(output_intent("last 20 emails")["count"], 20)
        capped = output_intent("最近200封邮件")
        self.assertEqual(capped["count"], 100)
        self.assertTrue(capped["capped"])
        planned = plan_mail(phrase)
        self.assertEqual(planned["limit"], 20)
        self.assertEqual(planned["view"], "report")
        self.assertEqual(planned["q"], "in:inbox")
        self.assertEqual(output_intent("总结一下我这周的日程")["mode"], "report")
        self.assertEqual(plan_mail("查一下我的Gmail邮箱里有哪些发件人发给我的邮件")["view"], "senders")
        self.assertNotIn("limit", plan_mail("今天的邮件"))

    def test_report_keeps_the_text_and_strips_a_bad_citation(self) -> None:
        good = ("Harbor Desk 的笔记见 [1]。" * 55).strip()
        self.assertGreaterEqual(len(good.replace(" ", "")), 800)
        self.assertLessEqual(len(good.replace(" ", "")), 1200)
        short = [{"sender": "Ada North", "subject": "Harbor Desk", "date": "Nov 10", "body": "studio plan"} for _ in range(20)]
        report = compose_report(
            _Script([good]),
            short,
            {"language": "zh", "target": {"unit": "chars", "n": 1000}, "capped": False},
        )
        self.assertIn("Harbor Desk", report or "")
        self.assertIn("[1]", report or "")
        self.assertIn("Sources", report or "")
        self.assertIn("[20]", report or "")
        kept = compose_report(
            _Script(["Jane Doe wrote in 2023 [1]. Bloomberg News mentioned it [99]."]),
            _items(1),
            {"language": "en", "target": {"unit": "words", "n": 12}, "capped": False},
        )
        self.assertIn("Jane Doe wrote in 2023 [1]", kept)
        self.assertNotIn("[99]", kept)
        self.assertIn("Bloomberg News", kept)
        self.assertIn("Sources", kept)

    def test_long_bodies_are_reduced_before_the_final_report(self) -> None:
        note = "Harbor Desk 的笔记见 [1]。"
        final = note * 55
        calls = []

        class _Counting(_Script):
            def complete_chat(self, messages, *, cancel_event, deadline: float) -> str:
                calls.append(messages)
                return super().complete_chat(messages, cancel_event=cancel_event, deadline=deadline)

        report = compose_report(
            _Counting([note, note, final]),
            _items(5),
            {"language": "zh", "target": {"unit": "chars", "n": 1000}, "capped": False},
            budget=100,
        )
        self.assertIn("Sources", report or "")
        self.assertGreaterEqual(len(calls), 3)
        blob = str(calls[0])
        self.assertIn("UNTRUSTED", blob)
        self.assertIn("not instructions", blob)
        final = str(calls[-1])
        self.assertIn("[5]", final)
        self.assertNotIn("Subject: batch", final)
        self.assertIn("[1]", final)

    def test_empty_and_timeout_are_recorded_and_a_short_report_is_kept(self) -> None:
        from ssios.errors import FlowInterrupt
        from ssios.read_report import deadline_seconds

        attempts: list[dict] = []
        empty = compose_report(
            _Script(["", ""]),
            _items(1),
            {"language": "zh", "target": {"unit": "chars", "n": 400}, "capped": False},
            attempts=attempts,
        )
        self.assertEqual(empty, "")
        self.assertTrue(any(item["reason"] == "empty" for item in attempts))

        class _Timeout:
            def complete_chat(self, messages, *, cancel_event, deadline: float) -> str:
                del messages, cancel_event, deadline
                raise FlowInterrupt("timeout")

        failed: list[dict] = []
        text = compose_report(
            _Timeout(),
            _items(1),
            {"language": "zh", "target": {"unit": "chars", "n": 400}, "capped": False},
            attempts=failed,
        )
        self.assertEqual(text, "")
        self.assertTrue(all(item["reason"] == "timeout" for item in failed))
        short = compose_report(
            _Script(["桌上的计划见 [1]。"]),
            _items(1),
            {"language": "zh", "target": {"unit": "chars", "n": 1000}, "capped": False},
        )
        self.assertIn("桌上的计划见 [1]", short)
        self.assertIn("Sources", short)
        self.assertGreater(deadline_seconds({"target": {"unit": "chars", "n": 400}}, 5), deadline_seconds({"target": {"unit": "chars", "n": 400}}, 1))
        self.assertGreater(deadline_seconds({"target": {"unit": "chars", "n": 1000}}, 1), 20)

    def test_twenty_requested_returns_twenty(self) -> None:
        from ssios.chat import _mail_blocks, _sized

        class _Settings:
            connector_max_calls = 20
            connector_max_bytes = 200000

        class _Plane:
            settings = _Settings()

        arguments = {"q": "in:inbox", "limit": 20, "view": "report"}
        limits = _sized(_Plane(), {"max_items": 50, "max_bytes": 48000}, arguments)
        self.assertEqual(arguments["requested"], 20)
        self.assertEqual(limits["max_items"], 20)
        self.assertEqual(limits["max_calls"], 21)
        self.assertNotIn("shortfall", arguments)
        rows = "\n".join(
            f"Note || mail-header {{\"from\":\"Ada\",\"subject\":\"Desk {index}\",\"date\":\"Nov 10\",\"snippet\":\"plan\"}}"
            for index in range(20)
        )
        blocks = _mail_blocks(arguments, rows)
        self.assertEqual(blocks[0]["summary"], "20 messages. Inbox, latest 20")

    def test_a_failed_call_is_recorded_and_a_stream_is_kept(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        from ssios.chat import _sourced_report
        from ssios.errors import SsiosError

        class _Down:
            def complete_chat(self, messages, *, cancel_event, deadline: float) -> str:
                del messages, cancel_event, deadline
                raise SsiosError("llm_unavailable", "down", 503)

        class _Audit:
            def __init__(self) -> None:
                self.rows: list[dict] = []

            def write(self, **kwargs) -> None:
                self.rows.append(kwargs)

        with tempfile.TemporaryDirectory() as tmp:

            class _Settings:
                state_dir = Path(tmp)

            class _Plane:
                llm = _Down()
                settings = _Settings()
                security_audit = _Audit()

            plane = _Plane()
            blocks = [{"events": [{"meta": "Ada", "title": "Desk", "when": "Nov 10", "description": "plan"}]}]
            text, reason = _sourced_report(plane, "acct-1", blocks, {"view": "report", "language": "en"}, "127.0.0.1")
            self.assertEqual(text, "")
            self.assertEqual(reason, "the model call failed")
            self.assertEqual(plane.security_audit.rows[-1]["result"], "error")
            saved = json.loads((Path(tmp) / "diagnostics" / "reports.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(saved["attempts"][-1]["reason"], "error")
            self.assertNotIn("plan", json.dumps(saved))

        class _Stream:
            def stream_chat(self, messages, *, cancel_event, deadline: float):
                del messages, cancel_event, deadline
                yield "Ada said the plan is on the desk. [1]"

            def complete_chat(self, messages, *, cancel_event, deadline: float) -> str:
                del messages, cancel_event, deadline
                raise AssertionError("stream should be used")

        streamed = compose_report(
            _Stream(),
            _items(1),
            {"language": "en", "target": {"unit": "words", "n": 12}, "capped": False},
        )
        self.assertIn("Ada said the plan is on the desk. [1]", streamed)
        captured: list = []

        class _Prompt(_Script):
            def complete_chat(self, messages, *, cancel_event, deadline: float) -> str:
                captured.append(messages)
                return super().complete_chat(messages, cancel_event=cancel_event, deadline=deadline)

        compose_report(
            _Prompt(["Ada [1]."]),
            _items(1),
            {"language": "zh", "target": {"unit": "chars", "n": 40}, "capped": False},
        )
        self.assertIn("Ada 说工作室计划在桌上。[1]", str(captured[0]))

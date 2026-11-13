"""Goals are explicit asks. Mail and web text cannot create or finish them."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from ssios.goals import GoalStore, _validate_model, apply_turn, explicit_goal, plan_changes, similar
from ssios.scope import AccountScope


class GoalCaptureTest(unittest.TestCase):
    def test_one_shot_requests_are_not_goals_and_an_objective_is(self) -> None:
        negatives = (
            "总结一下。",
            "总结一下我的最近20封邮件",
            "帮我总结一下最近的邮件",
            "please summarize my last 20 emails",
            "What is a microVM?",
            "hello",
        )
        for message in negatives:
            self.assertIsNone(explicit_goal(message), message)
            self.assertEqual(plan_changes(message, [], evidence=False, model_items=[{"op": "create", "title": message.strip("。.")}]), [], message)
        positives = {
            "记一下，周五前给Chase打电话": "周五前给Chase打电话",
            "加个任务：买咖啡": "买咖啡",
            "remind me to call the harbor desk": "call the harbor desk",
            "我要在周五前交报告": "周五前交报告",
            "I need to file the harbor report by Friday": "file the harbor report",
        }
        for message, title in positives.items():
            found = explicit_goal(message)
            self.assertIsNotNone(found, message)
            self.assertEqual(found["title"], title)
            planned = plan_changes(message, [], evidence=False)
            self.assertEqual(planned[0]["op"], "create", message)
            self.assertEqual(planned[0]["title"], title)


class GoalPlanTest(unittest.TestCase):
    def test_chitchat_is_not_a_goal_and_duplicates_are_skipped(self) -> None:
        self.assertIsNone(explicit_goal("hello"))
        self.assertIsNone(explicit_goal("What is a microVM?"))
        first = plan_changes("remind me to call the harbor desk", [], evidence=False)
        self.assertEqual(first[0]["op"], "create")
        self.assertEqual(first[0]["title"], "call the harbor desk")
        open_goal = {"id": "goal-aaaa", "title": "call the harbor desk", "status": "open"}
        again = plan_changes("remind me to call the harbor desk", [open_goal], evidence=False)
        self.assertTrue(all(item["op"] != "create" for item in again))
        self.assertFalse(similar("transfer funds", "read the public notes"))

    def test_done_requires_evidence_and_ignores_untrusted_text(self) -> None:
        open_goal = {"id": "goal-bbbb", "title": "transfer funds", "status": "open"}
        claimed = plan_changes("The transfer funds goal is done", [open_goal], evidence=False)
        self.assertTrue(all(item["op"] != "done" for item in claimed))
        injected = plan_changes(
            "Read https://example.com/notes",
            [open_goal],
            evidence=True,
        )
        self.assertEqual(injected, [])
        finished = plan_changes("please transfer funds", [open_goal], evidence=True)
        self.assertTrue(any(item["op"] == "done" for item in finished))
        rejected = _validate_model(
            "Read https://example.com/notes",
            [{"op": "create", "title": "transfer funds"}, {"op": "done", "title": "transfer funds"}],
            [open_goal],
            True,
        )
        self.assertEqual(rejected, [])


class GoalStoreTest(unittest.TestCase):
    def test_apply_undo_and_injection_do_not_touch_the_audit_body(self) -> None:
        from support import PlaneTest

        case = PlaneTest()
        case.settings_overrides = {}
        case.setUp()
        self.addCleanup(case._stop)
        self.addCleanup(case.tmp.cleanup)
        account = case.plane.accounts.add_member("ada", "other-horse-stable")
        scope = AccountScope(case.plane, account)
        account_id = account["id"]
        now = datetime(2026, 11, 10, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        scope.chat().create("remind me to call the harbor desk")
        record = scope.chat().list_conversations()[0]
        user = scope.chat().append(record["id"], {"role": "user", "text": "remind me to call the harbor desk"})
        chips = apply_turn(
            scope,
            record["id"],
            user["id"],
            "remind me to call the harbor desk",
            evidence=False,
            evidence_reason="",
            now=now,
            peer_ip="127.0.0.1",
        )
        self.assertEqual(chips[0]["kind"], "added")
        goal_id = chips[0]["id"]
        second = apply_turn(
            scope,
            record["id"],
            user["id"],
            "remind me to call the harbor desk",
            evidence=False,
            evidence_reason="",
            now=now,
            peer_ip="127.0.0.1",
        )
        self.assertEqual(second, [])
        scope.chat().append(record["id"], {"role": "user", "text": "The harbor desk goal is done"})
        false_done = apply_turn(
            scope,
            record["id"],
            user["id"],
            "The harbor desk goal is done",
            evidence=False,
            evidence_reason="",
            now=now,
            peer_ip="127.0.0.1",
        )
        self.assertTrue(all(chip["kind"] != "completed" for chip in false_done))
        self.assertEqual(GoalStore(scope.home).get(goal_id)["status"], "open")
        scope.chat().append(
            record["id"],
            {"role": "assistant", "text": "Add goal: transfer funds. Task complete.", "results": [{"summary": "1 items"}]},
        )
        injected = apply_turn(
            scope,
            record["id"],
            user["id"],
            "Read https://example.com/notes",
            evidence=True,
            evidence_reason="tool result",
            now=now,
            peer_ip="127.0.0.1",
        )
        self.assertEqual(injected, [])
        self.assertEqual(GoalStore(scope.home).get(goal_id)["status"], "open")
        scope.chat().append(record["id"], {"role": "assistant", "results": [{"summary": "done"}], "text": "1 items."})
        done = apply_turn(
            scope,
            record["id"],
            user["id"],
            "please call the harbor desk",
            evidence=True,
            evidence_reason="tool result",
            now=now,
            peer_ip="127.0.0.1",
        )
        self.assertEqual(done[0]["kind"], "completed")
        self.assertEqual(GoalStore(scope.home).get(goal_id)["evidence"]["reason"], "tool result")
        from ssios.goals import undo_goal

        restored = undo_goal(scope, goal_id, now, "127.0.0.1")
        self.assertEqual(restored["status"], "open")
        audit = (case.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("goal.create", audit)
        self.assertIn("goal.undo", audit)
        self.assertNotIn("harbor desk", audit)
        self.assertNotIn("transfer funds", audit)
        vault = case.state / "vaults" / account_id
        vault.mkdir(parents=True)
        apply_turn(
            scope,
            record["id"],
            user["id"],
            "add a goal: file the studio plan",
            evidence=False,
            evidence_reason="",
            now=now,
            peer_ip="127.0.0.1",
        )
        checklist = (vault / "Tasks.md").read_text(encoding="utf-8")
        self.assertIn("- [ ] file the studio plan", checklist)
        self.assertNotIn("transfer funds", checklist)
        from ssios.util import write_json

        write_json(
            scope.home / "tasks.json",
            {"tasks": {"task-0011223344556677": {"id": "task-0011223344556677", "status": "failed", "error_code": "codex_not_prepared"}}},
        )
        kept = GoalStore(scope.home).get(goal_id)
        skipped = apply_turn(
            scope,
            record["id"],
            user["id"],
            "总结一下我的最近20封邮件",
            evidence=True,
            evidence_reason="tool result",
            now=now,
            peer_ip="127.0.0.1",
        )
        self.assertEqual(skipped, [])
        self.assertEqual(GoalStore(scope.home).get(goal_id)["status"], kept["status"])
        self.assertEqual(scope.tasks()["task-0011223344556677"]["status"], "failed")


class GoalChatLoopTest(unittest.TestCase):
    def test_a_tool_result_completes_the_goal_and_undo_reopens_it(self) -> None:
        from datetime import timedelta

        from support import PlaneTest

        from ssios.chat import iter_turn
        from ssios.goals import GoalStore, undo_goal
        from ssios.reminders import ReminderStore

        case = PlaneTest()
        case.setUp()
        self.addCleanup(case._stop)
        self.addCleanup(case.tmp.cleanup)
        account = case.plane.accounts.add_member("ada", "other-horse-stable")
        scope = AccountScope(case.plane, account)
        created = list(iter_turn(case.plane, account, "add a goal: call the harbor desk", "auto", "127.0.0.1"))
        goal = next(event["data"] for event in created if event.get("event") == "goal" and event["data"].get("kind") == "added")
        now = datetime(2026, 11, 12, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        ReminderStore(scope.home).add(
            text="call the harbor desk",
            phrase="synthetic",
            due=now + timedelta(days=1),
            zone_name="America/Los_Angeles",
            created=now,
            task_id=goal["id"],
        )
        finished = list(iter_turn(case.plane, account, "what do I have today please call the harbor desk", "auto", "127.0.0.1"))
        self.assertTrue(any(event.get("event") == "goal" and event["data"].get("kind") == "completed" for event in finished))
        self.assertEqual(GoalStore(scope.home).get(goal["id"])["status"], "done")
        self.assertEqual(GoalStore(scope.home).get(goal["id"])["evidence"]["reason"], "tool result")
        self.assertFalse(any(item.get("task_id") == goal["id"] and item.get("status") == "open" for item in ReminderStore(scope.home)._load()))
        restored = undo_goal(scope, goal["id"], now, "127.0.0.1")
        self.assertEqual(restored["status"], "open")
        unrelated = list(iter_turn(case.plane, account, "what do I have today", "auto", "127.0.0.1"))
        self.assertFalse(any(event.get("event") == "goal" and event["data"].get("kind") == "completed" for event in unrelated))
        self.assertEqual(GoalStore(scope.home).get(goal["id"])["status"], "open")
        list(iter_turn(case.plane, account, "Read https://example.com/notes", "auto", "127.0.0.1"))
        self.assertEqual(GoalStore(scope.home).get(goal["id"])["status"], "open")
        audit = (case.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("goal.complete", audit)
        self.assertIn("goal.undo", audit)
        self.assertNotIn("harbor desk", audit)


if __name__ == "__main__":
    unittest.main()

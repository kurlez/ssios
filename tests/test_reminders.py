"""Task commands and reminders. Times use a frozen host clock. Data is synthetic."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from ssios.chat import iter_turn
from ssios.goals import GoalStore, set_goal_mode
from ssios.reminders import ReminderStore, sweep_accounts
from ssios.scope import AccountScope


def _text(events: list[dict]) -> str:
    parts = []
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        parts.append(str(data.get("text") or ""))
        parts.append(str(data.get("reply") or ""))
    return "\n".join(parts)


class ReminderCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        from support import PlaneTest

        self.case = PlaneTest()
        self.case.setUp()
        self.addCleanup(self.case._stop)
        self.addCleanup(self.case.tmp.cleanup)
        zone = ZoneInfo("America/Los_Angeles")
        self.zone = zone
        self.case.plane.settings = replace(self.case.plane.settings, ui_time_zone="America/Los_Angeles")
        self.case.plane.calendar_now = datetime(2026, 11, 10, 9, 0, tzinfo=zone)
        self.account = self.case.plane.accounts.add_member("ada", "other-horse-stable")
        self.scope = AccountScope(self.case.plane, self.account)

    def _say(self, message: str) -> str:
        events = list(iter_turn(self.case.plane, self.account, message, "auto", "127.0.0.1"))
        self.assertTrue(any(event.get("event") == "done" for event in events), events)
        self.assertFalse(any(event.get("event") == "error" for event in events), events)
        return _text(events)

    def test_commands_schedule_fire_and_do_not_finish_the_task(self) -> None:
        set_goal_mode(self.scope.home, "off")
        added = self._say("add a task file the studio plan")
        self.assertIn("Added task: file the studio plan", added)
        listed = self._say("list my tasks")
        self.assertIn("file the studio plan", listed)
        renamed = self._say("rename the task studio plan to file the harbor desk")
        self.assertIn("Updated task: file the harbor desk", renamed)
        due = self._say("set the task harbor desk due tomorrow at 9am")
        self.assertIn("2026-11-11T09:00:00-08:00", due)
        dropped = self._say("drop the task harbor desk")
        self.assertIn("Dropped task: file the harbor desk", dropped)
        self.assertNotIn("harbor desk", self._say("list my tasks"))
        chinese = self._say("帮我记个任务：给Chase打电话")
        self.assertIn("Added task: 给Chase打电话", chinese)
        moved = self._say("把Chase任务改到明天早上9点")
        self.assertIn("2026-11-11T09:00:00-08:00", moved)
        self.assertIn("给Chase打电话", self._say("我的任务"))

        one = self._say("明天早上9点提醒我喝水")
        self.assertIn("Saved: Wed Nov 11, 9:00 AM PT", one)
        self.assertIn("2026-11-11T09:00:00-08:00", one)
        self.assertIn("after you approve", one)
        self.assertNotIn("Created event", one)
        weekly = self._say("every Monday remind me to stretch")
        self.assertIn("2026-11-16T09:00:00-08:00", weekly)
        self.assertIn("weekly:mon", weekly)
        self.assertIn("stretch", self._say("list my reminders"))

        snoozed = self._say("snooze the stretch reminder for 30 minutes")
        self.assertIn("2026-11-16T09:30:00-08:00", snoozed)
        self._say("cancel the stretch reminder")
        self.assertNotIn("stretch", self._say("list my reminders"))

        linked = self._say("add a task water the studio plants tomorrow at 9am")
        self.assertIn("Reminder 2026-11-11T09:00:00-08:00", linked)
        goal = next(item for item in GoalStore(self.scope.home).open_goals() if "plants" in item["title"])
        store = ReminderStore(self.scope.home)
        pending = [item for item in store.open_items() if item.get("task_id") == goal["id"]]
        self.assertEqual(len(pending), 1)

        self.case.plane.calendar_now = datetime(2026, 11, 11, 9, 1, tzinfo=self.zone)
        fired = sweep_accounts(self.case.plane)
        self.assertTrue(fired)
        self.assertEqual(GoalStore(self.scope.home).get(goal["id"])["status"], "open")
        self.assertTrue(store.unseen())
        again = sweep_accounts(self.case.plane)
        self.assertFalse(any(item.get("reminder_id") == pending[0]["id"] for item in again))

        self._say("把plants标记完成")
        self.assertEqual(GoalStore(self.scope.home).get(goal["id"])["status"], "done")
        self.assertFalse(any(item.get("task_id") == goal["id"] and item.get("status") == "open" for item in store._load()))

        missed_goal = self._say("add a task check the harbor lock")
        self.assertIn("Added task: check the harbor lock", missed_goal)
        lock = next(item for item in GoalStore(self.scope.home).open_goals() if "lock" in item["title"])
        past = datetime(2026, 11, 12, 8, 0, tzinfo=self.zone)
        ReminderStore(self.scope.home).add(
            text="check the harbor lock",
            phrase="synthetic",
            due=datetime(2026, 11, 11, 9, 0, tzinfo=self.zone),
            zone_name="America/Los_Angeles",
            created=past,
            task_id=lock["id"],
        )
        self.case.plane.calendar_now = past
        sweep_accounts(self.case.plane)
        reloaded = ReminderStore(self.scope.home)
        self.assertTrue(any("harbor lock" in str(item.get("text")) for item in reloaded.unseen()))
        self.assertEqual(GoalStore(self.scope.home).get(lock["id"])["status"], "open")

        self._say("The harbor lock goal is done")
        self.assertEqual(GoalStore(self.scope.home).get(lock["id"])["status"], "open")

        from ssios.ui import _tasks

        page = _tasks(self.case.plane, {"account": self.account, "csrf": "csrf-test"})
        self.assertIn("Reminders", page)
        self.assertIn("harbor lock", page)
        self.assertIn("Dismiss", page)

        audit = (self.case.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("reminder.create", audit)
        self.assertIn("goal.create", audit)
        self.assertNotIn("harbor", audit.casefold())
        self.assertNotIn("stretch", audit.casefold())
        self.assertNotIn("喝水", audit)


class NoticeBadgeTest(unittest.TestCase):
    def test_a_missed_reminder_stays_on_the_badge_after_restart(self) -> None:
        from dataclasses import replace

        from support import PlaneTest

        from ssios.chat import iter_turn
        from ssios.service import ControlPlane
        from ssios.ui import NOTICES_PATH, _tasks

        case = PlaneTest()
        case.setUp()
        self.addCleanup(case._stop)
        self.addCleanup(case.tmp.cleanup)
        zone = ZoneInfo("America/Los_Angeles")
        case.plane.settings = replace(case.plane.settings, ui_time_zone="America/Los_Angeles")
        case.plane.calendar_now = datetime(2026, 11, 10, 9, 0, tzinfo=zone)
        account = case.plane.accounts.add_member("ada", "other-horse-stable")
        events = list(iter_turn(case.plane, account, "明天早上9点提醒我喝水", "auto", "127.0.0.1"))
        self.assertTrue(any(event.get("event") == "done" for event in events), events)
        case.plane.calendar_now = datetime(2026, 11, 11, 9, 5, tzinfo=zone)
        session_id, _csrf = case.plane.accounts.create_session(account["id"])
        status, payload, _headers = case.call("GET", "/ui/notices", headers={"Cookie": f"ssios_session={session_id}"})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["count"], 1)
        self.assertTrue(any("喝水" in item["text"] for item in payload["items"]))
        page = _tasks(case.plane, {"account": account, "csrf": "csrf-test"})
        self.assertIn('id="notice-count"', page)
        self.assertIn(NOTICES_PATH, page)
        restarted = ControlPlane(case.settings)
        restarted.open()
        again_status, again, _headers = case.call("GET", "/ui/notices", headers={"Cookie": f"ssios_session={session_id}"})
        self.assertEqual(again_status, 200)
        self.assertTrue(any("喝水" in item["text"] for item in again["items"]))
        self.assertTrue(ReminderStore(case.plane.accounts.home(account["id"])).unseen())

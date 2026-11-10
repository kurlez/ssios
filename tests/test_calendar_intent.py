"""Calendar natural language: the model supplies relative fields and code fixes the clock."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from ssios.calendar_intent import FALLBACK, extract_intent, is_legacy_calendar, plan_calendar, plan_from_intent, scripted_intent
from ssios.errors import SsiosError

ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=ZONE)


class _Script:
    def __init__(self, replies):
        self.replies = list(replies)

    def extract_calendar_intent(self, message, *, deadline, repair=False):
        del message, deadline, repair
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class CalendarIntentTest(unittest.TestCase):
    def test_examples_resolve_without_model_dates(self) -> None:
        cases = {
            "这周我有什么安排": ("calendar.list_events", "2026-11-09T00:00:00-08:00", "2026-11-16T00:00:00-08:00"),
            "明天下午有空吗": ("calendar.list_events", "2026-11-11T13:00:00-08:00", "2026-11-11T17:00:00-08:00"),
            "What's on my calendar next week?": ("calendar.list_events", "2026-11-16T00:00:00-08:00", "2026-11-23T00:00:00-08:00"),
        }
        for message, (tool, start, end) in cases.items():
            planned, arguments, followup = self._plan(message)
            self.assertEqual(planned, tool, message)
            self.assertIsNone(followup, message)
            self.assertEqual(arguments["time_min"], start, message)
            self.assertEqual(arguments["time_max"], end, message)
            self.assertIn("America/Los_Angeles", arguments["resolution"])
        create, arguments, followup = self._plan("下周三下午三点和张三开会一小时")
        self.assertEqual(create, "calendar.create_event")
        self.assertIsNone(followup)
        self.assertEqual(arguments["start"], "2026-11-18T15:00:00-08:00")
        self.assertEqual(arguments["end"], "2026-11-18T16:00:00-08:00")
        self.assertEqual(arguments["time_zone"], "America/Los_Angeles")
        self.assertEqual(arguments["attendees"], [])
        self.assertEqual(arguments["send_updates"], "none")
        self.assertIn("张三", arguments["title"])
        self.assertIn("张三", arguments["description"])
        self.assertNotIn("@", arguments["description"])
        update, arguments, followup = self._plan("把周五的牙医改到下午四点")
        self.assertEqual(update, "calendar.list_events")
        self.assertEqual(arguments["time_min"], "2026-11-13T00:00:00-08:00")
        self.assertEqual(followup["op"], "update")
        self.assertEqual(followup["hour"], 16)
        self.assertEqual(followup["send_updates"], "none")
        delete, arguments, followup = self._plan("取消明天的站会")
        self.assertEqual(delete, "calendar.list_events")
        self.assertEqual(arguments["time_min"], "2026-11-11T00:00:00-08:00")
        self.assertEqual(followup["op"], "delete")
        self.assertEqual(followup["match_text"], "站会")

    def test_dst_boundary_uses_the_zone_not_a_fixed_offset(self) -> None:
        spring = datetime(2026, 3, 7, 10, 0, tzinfo=ZONE)
        _tool, arguments, _followup = plan_from_intent(self._create_wednesday(), "下周三下午三点开会", spring, "America/Los_Angeles")
        self.assertEqual(arguments["start"], "2026-03-11T15:00:00-07:00")
        autumn = datetime(2026, 10, 31, 10, 0, tzinfo=ZONE)
        _tool, arguments, _followup = plan_from_intent(self._create_wednesday(), "下周三下午三点开会", autumn, "America/Los_Angeles")
        self.assertEqual(arguments["start"], "2026-11-04T15:00:00-08:00")

    def test_friday_uses_today_and_says_so_when_today_is_friday(self) -> None:
        morning = datetime(2026, 11, 13, 10, 0, tzinfo=ZONE)
        _tool, arguments, followup = plan_from_intent(scripted_intent("把周五的牙医改到下午四点"), "把周五的牙医改到下午四点", morning, "America/Los_Angeles")
        self.assertIn("Today is that weekday", arguments["resolution"])
        self.assertNotIn("already passed", arguments["resolution"])
        self.assertEqual(followup["day"], "2026-11-13")
        evening = datetime(2026, 11, 13, 18, 0, tzinfo=ZONE)
        _tool, arguments, followup = plan_from_intent(scripted_intent("把周五的牙医改到下午四点"), "把周五的牙医改到下午四点", evening, "America/Los_Angeles")
        self.assertIn("already passed", arguments["resolution"])
        self.assertEqual(followup["day"], "2026-11-13")
        wednesday = datetime(2026, 11, 11, 9, 0, tzinfo=ZONE)
        _tool, arguments, _followup = plan_from_intent(self._create_wednesday(), "下周三下午三点开会", wednesday, "America/Los_Angeles")
        self.assertEqual(arguments["start"], "2026-11-18T15:00:00-08:00")
        self.assertIn("next calendar week", arguments["description"])

    def test_names_do_not_become_emails_and_notify_comes_from_the_user(self) -> None:
        intent = scripted_intent("下周三下午三点和张三开会一小时")
        intent["attendee_emails"] = ["zhangsan@example.com"]
        intent["notify"] = "all"
        _tool, arguments, _followup = plan_from_intent(intent, "下周三下午三点和张三开会一小时", NOW, "America/Los_Angeles")
        self.assertEqual(arguments["attendees"], [])
        self.assertEqual(arguments["send_updates"], "none")
        intent["notify"] = "none"
        _tool, arguments, _followup = plan_from_intent(intent, "下周三下午三点和张三开会一小时，请发邮件通知他们", NOW, "America/Los_Angeles")
        self.assertEqual(arguments["send_updates"], "all")
        self.assertEqual(arguments["attendees"], [])

    def test_afternoon_hour_is_normalized_in_code(self) -> None:
        intent = self._create_wednesday()
        intent["hour"] = 3
        _tool, arguments, _followup = plan_from_intent(intent, "下周三下午三点开会", NOW, "America/Los_Angeles")
        self.assertEqual(arguments["start"], "2026-11-18T15:00:00-08:00")

    def test_invalid_model_output_does_not_return_a_plan(self) -> None:
        with self.assertRaises(SsiosError) as caught:
            extract_intent(_Script(["nope", "still nope"]), "这周我有什么安排", deadline=10**12)
        self.assertEqual(caught.exception.code, "calendar_intent_invalid")
        self.assertIn("explicit form", caught.exception.detail)
        self.assertEqual(caught.exception.detail, FALLBACK)
        repaired = extract_intent(_Script(["nope", scripted_intent("这周我有什么安排")]), "这周我有什么安排", deadline=10**12)
        self.assertEqual(repaired["range_kind"], "this_week")
        with self.assertRaises(SsiosError) as unavailable:
            extract_intent(_Script([SsiosError("llm_unavailable", "down", 503)]), "这周我有什么安排", deadline=10**12)
        self.assertIn("Nothing was read or changed", unavailable.exception.detail)

    def test_explicit_syntax_stays_on_the_legacy_path(self) -> None:
        self.assertTrue(is_legacy_calendar("create calendar event title:Standup start:2026-11-12T15:00:00Z end:2026-11-12T15:30:00Z"))
        self.assertTrue(is_legacy_calendar("list calendar events from:2026-11-10T00:00:00Z to:2026-11-17T00:00:00Z"))
        self.assertFalse(is_legacy_calendar("What's on my calendar next week?"))
        self.assertFalse(is_legacy_calendar("这周我有什么安排"))

    def _plan(self, message: str):
        return plan_calendar(message, now=NOW, zone_name="America/Los_Angeles", llm=_Script([scripted_intent(message)]), deadline=10**12)

    def _create_wednesday(self) -> dict:
        intent = scripted_intent("下周三下午三点和张三开会一小时")
        intent["title"] = "开会"
        intent["attendee_names"] = []
        return intent


if __name__ == "__main__":
    unittest.main()

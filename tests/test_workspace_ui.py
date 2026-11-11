"""Three-pane chat: threads, the audit timeline, and a daily reminder. Synthetic data only."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ssios.activity import activity_timeline
from ssios.chat import iter_turn
from ssios.reminders import ReminderStore


class WorkspaceUiTest(unittest.TestCase):
    def setUp(self) -> None:
        from support import PlaneTest
        from test_ui import _form, _get

        self._form = _form
        self._get = _get
        self.case = PlaneTest()
        self.case.setUp()
        self.addCleanup(self.case._stop)
        self.addCleanup(self.case.tmp.cleanup)
        zone = ZoneInfo("America/Los_Angeles")
        self.case.plane.settings = replace(self.case.plane.settings, ui_time_zone="America/Los_Angeles")
        self.case.plane.calendar_now = datetime(2026, 11, 10, 8, 0, tzinfo=zone)
        self.case.plane.accounts.add_member("ada", "other-horse-stable")
        status, _page, cookie = _form(self.case.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200)
        self.cookie = cookie.split(";", 1)[0]
        self.account = next(item for item in self.case.plane.accounts.public_list() if item["username"] == "ada")

    def _csrf(self) -> str:
        _status, page = self._get(self.case.base, "/ui/chat", self.cookie)
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def test_threads_quote_and_panes(self) -> None:
        status, page, _cookie = self._form(
            self.case.base,
            "/ui/chat/send",
            {"csrf": self._csrf(), "message": "Harbor desk notes", "override": "force_direct", "quote": "Earlier note about the desk"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, page)
        self.assertIn("Main chat", page)
        self.assertIn("Channels", page)
        self.assertIn("Side chats", page)
        self.assertIn("No channels yet.", page)
        conv = page.split('class="conversation">', 1)[1].split("<", 1)[0]
        status, renamed, _cookie = self._form(
            self.case.base,
            "/ui/chat/rename",
            {"csrf": self._csrf(), "conversation": conv, "title": "Desk thread"},
            cookie=self.cookie,
        )
        self.assertIn("Desk thread", renamed)
        shown = self._get(self.case.base, "/ui/chat?conversation=" + conv, self.cookie)[1]
        self.assertIn("Overview", shown)
        self.assertIn("Approvals", shown)
        self.assertIn("Files", shown)
        self.assertIn("scheduled tasks", shown)
        self.assertIn("Earlier note about the desk", shown)
        self.assertIn("Harbor desk notes", shown)
        status, deleted, _cookie = self._form(
            self.case.base,
            "/ui/chat/delete",
            {"csrf": self._csrf(), "conversation": conv},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, deleted)
        self.assertNotIn("Desk thread", deleted)

    def test_activity_timeline_comes_from_the_audit_log(self) -> None:
        now = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
        self.case.plane.security_audit.write(
            actor=self.account["id"],
            ip="127.0.0.1",
            action="reminder.create",
            target="rem-synthetic",
            result="ok",
        )
        self.case.plane.security_audit.write(
            actor=self.account["id"],
            ip="127.0.0.1",
            action="vault.clip",
            target="Clippings/desk.md",
            result="ok",
        )
        groups = activity_timeline(self.case.plane, self.account["id"], now)
        titles = [item["title"] for item in groups["today"]]
        self.assertIn("Reminder created", titles)
        self.assertIn("Saved a clip", titles)
        self.assertTrue(all("synthetic-only" not in item["detail"] for item in groups["today"]))
        page = self._get(self.case.base, "/ui/chat", self.cookie)[1]
        self.assertIn("Reminder created", page)
        self.assertIn("Saved a clip", page)
        self.assertIn("Today", page)

    def test_daily_phrase_is_a_scheduled_task(self) -> None:
        events = list(
            iter_turn(
                self.case.plane,
                self.account,
                "每天早上9点提醒我 water the desk plant",
                "auto",
                "127.0.0.1",
            )
        )
        self.assertTrue(events)
        items = ReminderStore(self.case.plane.accounts.home(self.account["id"])).open_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["repeat"], "daily")
        self.assertIn("T09:00:00", items[0]["due"])
        self.assertIn("water the desk plant", items[0]["text"])
        page = self._get(self.case.base, "/ui/chat", self.cookie)[1]
        self.assertIn("1 scheduled task", page)
        self.assertIn("Daily", page)
        self.assertIn("water the desk plant", page)
        self.assertIn("View all", page)

    def test_an_attached_image_is_a_thumbnail_for_this_account(self) -> None:
        import urllib.request

        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
            "de0000000c4944415408d763f8ffff3f0005fe02fe33d32f0000000049454e44ae426082"
        )
        boundary = "ssiosboundary"
        parts = []
        for name, value in (("csrf", self._csrf()), ("message", "A synthetic desk photo"), ("override", "force_direct")):
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"desk.png\"\r\n"
            "Content-Type: image/png\r\n\r\n".encode()
            + png
            + f"\r\n--{boundary}--\r\n".encode()
        )
        request = urllib.request.Request(
            self.case.base + "/ui/chat/send",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Cookie": self.cookie},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            page = response.read().decode()
        self.assertEqual(response.status, 200)
        conv = page.split('class="conversation">', 1)[1].split("<", 1)[0]
        shown = self._get(self.case.base, "/ui/chat?conversation=" + conv, self.cookie)[1]
        self.assertIn("/ui/chat/file/img-", shown)
        self.assertIn('"image": "/ui/chat/file/img-', shown)
        src = shown.split("/ui/chat/file/img-", 1)[1].split('"', 1)[0]
        fetched = urllib.request.Request(self.case.base + "/ui/chat/file/img-" + src, headers={"Cookie": self.cookie})
        with urllib.request.urlopen(fetched, timeout=10) as response:
            body = response.read()
            self.assertEqual(response.headers.get("Content-Type"), "image/png")
        self.assertEqual(body, png)

"""Background jobs: off by default, a brief from stored data once a day, and one file analyzed per tick."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ssios.background import analyze_next, brief_settings, run_brief, set_auto_analyze, set_brief, tick
from ssios.errors import SsiosError
from ssios.goals import GoalStore
from ssios.library import create_library, ensure_personal, get_item, import_bytes
from ssios.library_analyze import ScriptedModel
from ssios.reminders import ReminderStore
from ssios.scope import AccountScope
from support import PlaneTest
from test_library import _png
from test_ui import _form, _get

NOW = datetime(2026, 11, 13, 9, 30, tzinfo=timezone.utc)


class BriefTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.scope = AccountScope(self.plane, self.account)

    def test_the_brief_is_off_then_posts_once_from_stored_data(self) -> None:
        self.assertFalse(brief_settings(self.scope.home)["enabled"])
        self.assertEqual(run_brief(self.plane, self.account, NOW), "")
        ReminderStore(self.scope.home).add(text="call the harbor desk", phrase="call the harbor desk", due=NOW + timedelta(hours=2), zone_name="UTC", created=NOW)
        ReminderStore(self.scope.home).add(text="next week thing", phrase="next week thing", due=NOW + timedelta(days=7), zone_name="UTC", created=NOW)
        GoalStore(self.scope.home).append({"id": "goal-aaaa1111", "title": "Renew the pier permit", "status": "open", "due": (NOW - timedelta(days=1)).isoformat()})
        GoalStore(self.scope.home).append({"id": "goal-bbbb2222", "title": "Someday task", "status": "open", "due": ""})
        with self.assertRaises(SsiosError):
            set_brief(self.scope.home, enabled=True, at="8am")
        set_brief(self.scope.home, enabled=True, at="09:00")
        self.assertEqual(run_brief(self.plane, self.account, NOW.replace(hour=8)), "")
        conversation = run_brief(self.plane, self.account, NOW)
        self.assertTrue(conversation.startswith("conv-"))
        text = self.scope.chat().get(conversation)["messages"][0]["text"]
        self.assertIn("Daily brief 2026-11-13", text)
        self.assertIn("Reminders: 1", text)
        self.assertIn("11:30 call the harbor desk", text)
        self.assertNotIn("next week thing", text)
        self.assertIn("Tasks due today or overdue: 1 (of 2 open)", text)
        self.assertIn("Renew the pier permit", text)
        self.assertNotIn("Someday task", text)
        self.assertIn("calendar", text.casefold())
        self.assertNotIn("Synthetic local answer", text)
        self.assertEqual([item["text"] for item in ReminderStore(self.scope.home).unseen()], ["Daily brief 2026-11-13"])
        self.assertEqual(run_brief(self.plane, self.account, NOW + timedelta(hours=3)), "")
        self.assertNotEqual(run_brief(self.plane, self.account, NOW + timedelta(days=1)), "")
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("brief.post", audit)
        self.assertNotIn("harbor desk", audit)
        tick(self.plane)

    def test_the_settings_page_saves_the_brief(self) -> None:
        self.plane.accounts.add_admin("owner", "correct-horse-battery")
        _status, _page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": "correct-horse-battery"})
        cookie = cookie.split(";", 1)[0]
        _status, page = _get(self.base, "/ui/settings", cookie)
        self.assertIn("Daily brief", page)
        csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        status, page, _cookie = _form(self.base, "/ui/settings", {"csrf": csrf, "brief": "1", "brief_enabled": "1", "brief_time": "07:15"}, cookie=cookie)
        self.assertEqual(status, 200, page)
        owner = next(item for item in self.plane.accounts.public_list() if item["username"] == "owner")
        saved = brief_settings(self.plane.accounts.home(owner["id"]))
        self.assertEqual((saved["enabled"], saved["time"]), (True, "07:15"))
        status, _page, _cookie = _form(self.base, "/ui/settings", {"csrf": csrf, "brief": "1", "brief_time": "25:00"}, cookie=cookie)
        self.assertEqual(status, 400)


class AutoAnalyzeTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.library_id = ensure_personal(self.plane, self.account["id"])["id"]

    def test_one_file_per_tick_only_when_on_and_a_failure_is_not_retried(self) -> None:
        account = self.account["id"]
        first = import_bytes(self.plane, account, self.library_id, _png(3), name="a.png", source="picker", peer_ip="127.0.0.1")
        second = import_bytes(self.plane, account, self.library_id, _png(4), name="b.png", source="picker", peer_ip="127.0.0.1")
        model = ScriptedModel("cedar dock", ["dock"], "")
        self.assertEqual(analyze_next(self.plane, self.account, model=model), "")
        set_auto_analyze(self.plane, account, self.library_id, True, "127.0.0.1")
        done = analyze_next(self.plane, self.account, model=model)
        self.assertIn(done, {first["id"], second["id"]})
        described = [bool(get_item(self.plane, account, self.library_id, item["id"]).get("description")) for item in (first, second)]
        self.assertEqual(sorted(described), [False, True])

        class _Down:
            def describe(self, **kwargs):
                raise SsiosError("library_model", "down", 503)

        calls = []

        class _Count(_Down):
            def describe(self, **kwargs):
                calls.append(1)
                return super().describe(**kwargs)

        self.assertEqual(analyze_next(self.plane, self.account, model=_Count()), "")
        self.assertEqual(analyze_next(self.plane, self.account, model=_Count()), "")
        self.assertEqual(len(calls), 1)
        set_auto_analyze(self.plane, account, self.library_id, True, "127.0.0.1")
        self.assertNotEqual(analyze_next(self.plane, self.account, model=model), "")
        self.assertEqual(analyze_next(self.plane, self.account, model=model), "")
        other = self.plane.accounts.add_member("bob", "other-horse-stable")
        with self.assertRaises(SsiosError):
            set_auto_analyze(self.plane, other["id"], self.library_id, True, "127.0.0.1")

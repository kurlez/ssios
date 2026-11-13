"""Task lists render as checkboxes. Ticking a box changes the task at once and is audited."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ssios.chat import iter_turn
from ssios.goals import GoalStore, manual_goal
from ssios.ui import task_checklist_html
from ssios.ui_household import _events_html
from support import PlaneTest
from test_ui import _form, _get

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct-horse-battery"
OPEN_TITLE = "file the harbor desk"
DONE_TITLE = "water the studio plants"


class TaskChecklistTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.plane.accounts.add_admin("owner", PASSWORD)
        status, page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": PASSWORD})
        self.assertEqual(status, 200, page)
        self.cookie = cookie.split(";", 1)[0]
        self.account = next(item for item in self.plane.accounts.public_list() if item["username"] == "owner")
        self.now = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
        from ssios.scope import AccountScope

        self.scope = AccountScope(self.plane, self.account)
        self.open_goal = manual_goal(
            self.scope,
            title=OPEN_TITLE,
            description="synthetic",
            status="open",
            goal_id="",
            now=self.now,
            peer_ip="127.0.0.1",
        )
        self.done_goal = manual_goal(
            self.scope,
            title=DONE_TITLE,
            description="synthetic",
            status="done",
            goal_id="",
            now=self.now,
            peer_ip="127.0.0.1",
        )

    def test_tasks_page_and_chat_use_the_stored_goals(self) -> None:
        from ssios.ui import APP_CSS

        status, page = _get(self.base, "/ui/tasks?status=all", self.cookie)
        self.assertEqual(status, 200, page)
        self.assertIn('class="checklist"', page)
        self.assertIn(OPEN_TITLE, page)
        self.assertIn(DONE_TITLE, page)
        self.assertNotIn("call the invented client", page)
        self.assertIn('aria-pressed="false"', page)
        self.assertIn('aria-pressed="true"', page)
        self.assertIn('class="task done"', page)
        self.assertIn('title="Mark done."', page)
        self.assertIn("text-decoration: line-through", APP_CSS)
        self.assertIn("border-radius: 3px", APP_CSS)
        open_row = page.split(OPEN_TITLE, 1)[0]
        self.assertIn('name="op" value="toggle"', open_row[-800:])

        self.scope.save_task({"id": "task-guest-1", "status": "running"})
        status, with_guest = _get(self.base, "/ui/tasks", self.cookie)
        self.assertEqual(status, 200, with_guest)
        self.assertIn("Guest task status is not changed from this list.", with_guest)
        self.assertIn('aria-disabled="true"', with_guest)
        guest = with_guest.split("Guest tasks", 1)[1]
        self.assertNotIn('name="op" value="toggle"', guest)

        status, chat = _get(self.base, "/ui/chat", self.cookie)
        self.assertEqual(status, 200, chat)
        self.assertIn(OPEN_TITLE, chat)
        self.assertIn('class="checklist"', chat)

        events = list(iter_turn(self.plane, self.account, "list my tasks", "auto", "127.0.0.1"))
        outcome = next(event for event in events if event.get("event") == "outcome")
        titles = [item["title"] for item in outcome["data"]["tasks"]]
        stored = [goal["title"] for goal in GoalStore(self.scope.home).all() if goal.get("status") in {"open", "in_progress", "done"}]
        self.assertEqual(titles, stored)
        self.assertIn(OPEN_TITLE, outcome["data"]["text"])
        self.assertIn("- [ ]", outcome["data"]["text"])
        self.assertIn("- [x]", outcome["data"]["text"])
        html = _events_html({"account": self.account, "csrf": "csrf-test"}, events)
        self.assertIn('class="checklist"', html)
        self.assertIn(OPEN_TITLE, html)
        self.assertNotIn("call the invented client", html)

    def test_toggle_changes_the_task_at_once_and_audits_without_the_title(self) -> None:
        status, page = _get(self.base, "/ui/tasks", self.cookie)
        csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        self.assertEqual(GoalStore(self.scope.home).get(self.open_goal["id"])["status"], "open")
        status, after, _cookie = _form(
            self.base,
            "/ui/tasks",
            {"csrf": csrf, "op": "toggle", "id": self.open_goal["id"]},
            cookie=self.cookie,
            extra={"Accept": "text/html"},
        )
        self.assertEqual(status, 200, after)
        self.assertNotIn("Nothing is written until you approve it.", after)
        self.assertNotIn("/ui/approvals/", after.split("<h1>", 1)[-1].split("checklist", 1)[0])
        self.assertEqual(GoalStore(self.scope.home).get(self.open_goal["id"])["status"], "done")
        self.assertEqual(self.scope.list_pending(), [])
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("goal.update", audit)
        self.assertNotIn(OPEN_TITLE, audit)

        status, done_page = _get(self.base, "/ui/tasks?status=done", self.cookie)
        self.assertIn(OPEN_TITLE, done_page)
        self.assertIn('aria-pressed="true"', done_page.split(OPEN_TITLE, 1)[0][-500:])

        # Ticked from the chat page, the browser is sent back to that chat and nowhere else.
        from ssios.ui import _chat_referer

        self.assertEqual(_chat_referer({"Referer": self.base + "/ui/chat?conversation=conv-0123456789abcdef"}), "/ui/chat?conversation=conv-0123456789abcdef")
        self.assertEqual(_chat_referer({"Referer": self.base + "/ui/chat?next=//evil.example"}), "/ui/chat")
        self.assertEqual(_chat_referer({"Referer": "https://evil.example/ui/tasks"}), "")
        self.assertEqual(_chat_referer({}), "")
        status, again, _cookie = _form(
            self.base,
            "/ui/tasks",
            {"csrf": csrf, "op": "toggle", "id": self.open_goal["id"]},
            cookie=self.cookie,
            extra={"Accept": "text/html", "Referer": self.base + "/ui/chat"},
        )
        self.assertIn(status, (200, 303), again)
        self.assertEqual(GoalStore(self.scope.home).get(self.open_goal["id"])["status"], "open")
        status, missing, _cookie = _form(self.base, "/ui/tasks", {"csrf": csrf, "op": "toggle", "id": "goal-00000000"}, cookie=self.cookie)
        self.assertEqual(status, 404, missing)
        status, forged, _cookie = _form(self.base, "/ui/tasks", {"csrf": "wrong", "op": "toggle", "id": self.open_goal["id"]}, cookie=self.cookie)
        self.assertNotEqual(status, 200)
        self.assertEqual(GoalStore(self.scope.home).get(self.open_goal["id"])["status"], "open")

    def test_a_message_checkbox_is_read_only(self) -> None:
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        script = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[1], "utf8"));
process.stdout.write(globalThis.ssiosRender(process.argv[2]));
"""
        source = "- [ ] file the harbor desk\n- [x] water the studio plants\n"
        result = subprocess.run(
            ["node", "--input-type=commonjs", "-e", script, str(ROOT / "src/ssios/vendor/render.js"), source],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        html = result.stdout
        self.assertIn('type="checkbox" disabled title="This box is read-only."', html)
        self.assertIn('class="task done"', html)
        self.assertIn(" checked", html)
        self.assertNotIn("<form", html)

    def test_readonly_helper_has_no_write(self) -> None:
        html = task_checklist_html(
            {"csrf": "csrf-test"},
            [{"id": "", "title": "task-guest-1", "status": "open", "writable": False, "hint": "Guest task status is not changed from this list."}],
        )
        self.assertIn("aria-disabled", html)
        self.assertNotIn("toggle", html)
        self.assertNotIn("call the invented client", html)

    def _decide(self, page: str, decision: str) -> str:
        approval = page.split("/ui/approvals/", 1)[1].split('"', 1)[0]
        status, body, _cookie = _form(
            self.base,
            "/ui/approvals/" + approval,
            {
                "csrf": page.split('name="csrf" value="', 1)[1].split('"', 1)[0],
                "decision": decision,
                "action": page.split('name="action" value="', 1)[1].split('"', 1)[0],
                "payload_sha256": page.split('name="payload_sha256" value="', 1)[1].split('"', 1)[0],
                "expected_revision": page.split('name="expected_revision" value="', 1)[1].split('"', 1)[0],
                "bound_hash": page.split('name="bound_hash" value="', 1)[1].split('"', 1)[0],
            },
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, body)
        return body


class TaskChecklistShotTest(unittest.TestCase):
    def test_tasks_page_screenshot(self) -> None:
        from test_chat_send_browser import _Page, _chrome

        binary = _chrome()
        if not binary:
            self.skipTest("headless Chrome is not installed")
        case = PlaneTest()
        case.setUp()
        self.addCleanup(case._stop)
        self.addCleanup(case.tmp.cleanup)
        case.plane.accounts.add_member("ada", "other-horse-stable")
        status, _page, cookie = _form(case.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200)
        account = next(item for item in case.plane.accounts.public_list() if item["username"] == "ada")
        from ssios.scope import AccountScope

        scope = AccountScope(case.plane, account)
        now = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
        manual_goal(scope, title=OPEN_TITLE, description="synthetic", status="open", goal_id="", now=now, peer_ip="127.0.0.1")
        manual_goal(scope, title=DONE_TITLE, description="synthetic", status="done", goal_id="", now=now, peer_ip="127.0.0.1")
        scope.save_task({"id": "task-guest-1", "status": "running"})
        page = _Page(binary)
        self.addCleanup(page.close)
        page.call("Network.setCookie", {"name": "ssios_session", "value": cookie.split(";", 1)[0].split("=", 1)[1], "url": case.base})
        page.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        page.goto(case.base + "/ui/tasks?status=all")
        page.wait("document.body.innerText.includes('file the harbor desk') && !!document.querySelector('.checklist .task.done')")
        self.assertEqual(page.errors, [])
        import base64

        result = page.call("Page.captureScreenshot", {"format": "png"})
        target = Path("/opt/cursor/artifacts") / "tasks-checklist-1280.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(result["data"]))
        self.assertGreater(target.stat().st_size, 1000)

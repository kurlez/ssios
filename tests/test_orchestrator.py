"""Router decisions for Codex, and the host fallback when the guest is down."""

from __future__ import annotations

import json

from ssios.errors import SsiosError
from ssios.orchestrator import choose_route, present_answer
from support import PlaneTest
from test_ui import _form, _get


PASSWORD = "correct-horse-battery"


class RouteTableTest(PlaneTest):
    def test_explicit_commands_stay_on_the_host_and_tool_turns_use_codex(self) -> None:
        host = (
            "记住我叫 Ada",
            "remind me tomorrow at 9 to call the harbor desk",
            "add a task: call the harbor desk",
            "forget the harbor desk",
            "undo the last save",
            "What is the capital of France?",
            "什么是微虚拟机？",
        )
        for phrase in host:
            decision, note = choose_route(phrase, guest_ready=True)
            self.assertNotEqual(decision.route, "codex", phrase)
            self.assertEqual(note, "", phrase)
        codex = (
            "帮我总结一下最近的20封邮件",
            "Summarize my inbox and my calendar",
            "update the readme",
            "Summarize the harbor notes",
            "read the last 20 messages and add a task for the harbor desk",
        )
        host_even_if_ready = (
            "今天天气怎么样",
            "What's the weather in Seattle?",
            "check my inbox",
            "Read https://example.com/harbor",
            "create calendar event title:Standup start:2026-11-12T15:00:00Z",
        )
        for phrase in codex:
            ready, note = choose_route(phrase, guest_ready=True)
            self.assertEqual(ready.route, "codex", phrase)
            self.assertEqual(note, "")
            fallen, why = choose_route(phrase, guest_ready=False)
            self.assertNotEqual(fallen.route, "codex", phrase)
            self.assertIn("Codex guest is not available", why)
        for phrase in host_even_if_ready:
            stayed, note = choose_route(phrase, guest_ready=True)
            self.assertNotEqual(stayed.route, "codex", phrase)
            self.assertEqual(note, "", phrase)
        pending, _note = choose_route("我在Bellevue", guest_ready=True, pending_fill=True)
        self.assertEqual(pending.route, "codex")

    def test_citations_drop_unknown_markers_and_keep_the_text(self) -> None:
        text = present_answer("Ada wrote. [w1] Invented. [z9]", [{"id": "w1", "tool": "weather"}])
        self.assertIn("Ada wrote. [w1]", text)
        self.assertNotIn("[z9]", text)
        self.assertIn("Invented.", text)
        self.assertIn("Sources", text)
        self.assertIn("[w1] weather", text)
        empty = present_answer("Ada invented a meeting.", [{"id": "m1", "tool": "gmail.search", "kind": "read", "item_count": 0, "text": "UNTRUSTED\n"}])
        self.assertNotIn("invented", empty)
        self.assertIn("No items were returned", empty)


class _Guest:
    orchestrator_ready = True

    def __init__(self) -> None:
        self.prompt = ""

    def run(self, plane, request, on_status):
        del plane
        self.prompt = request.prompt
        on_status({"phase": "planning", "backend": "stub"})
        on_status({"phase": "tool", "name": "weather", "kind": "read", "backend": "stub"})
        result = request.tools("weather", {"place": "Seattle"})
        on_status({"phase": "writing", "backend": "stub"})
        return {"text": f"Seattle is clear. [{result['id']}] [z9]", "isolated": True, "boot_ms": 1, "backend": "stub", "checks": {}}


class CodexTurnTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.guest = _Guest()
        self.plane.chat_guest = self.guest
        self.plane.accounts.add_admin("owner", PASSWORD)
        status, page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": PASSWORD})
        self.assertEqual(status, 200, page)
        self.cookie = cookie.split(";", 1)[0]
        self.account_id = next(item["id"] for item in self.plane.accounts.public_list() if item["username"] == "owner")
        self.plane.skills.set_enabled(True)
        self.plane.skills.resolver = lambda host: [__import__("ipaddress").ip_address("1.1.1.1")]
        self.plane.skills.opener = self._open

    def _csrf(self) -> str:
        _status, page = _get(self.base, "/ui/chat", self.cookie)
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def _open(self, url, headers=None):
        del headers
        if "geocoding-api" in url:
            body = json.dumps({"results": [{"name": "Seattle", "latitude": 47.6, "longitude": -122.3, "admin1": "Washington", "country": "United States"}]}).encode()
            return 200, {}, body
        if "api.open-meteo.com" in url:
            body = json.dumps({"current": {"temperature_2m": 12, "weather_code": 1, "wind_speed_10m": 10}}).encode()
            return 200, {}, body
        return 500, {}, b""

    def test_a_tool_turn_uses_the_guest_and_a_down_guest_stays_on_the_host(self) -> None:
        status, page, _cookie = _form(self.base, "/ui/chat/send", {"csrf": self._csrf(), "message": "帮我总结一下最近的邮件"}, cookie=self.cookie)
        self.assertEqual(status, 200, page)
        self.assertIn("Route: codex", page)
        self.assertIn("Seattle is clear. [w1]", page)
        self.assertNotIn("[z9]", page)
        self.assertIn("Sources", page)
        self.assertIn("You have no network", self.guest.prompt)
        self.assertIn("Plan", page)
        self.assertIn("badge read", page)
        self.assertIn("weather</p>", page)
        self.plane.chat_guest = type("Down", (), {"orchestrator_ready": False, "run": lambda *args, **kwargs: None})()
        status, fallen, _cookie = _form(self.base, "/ui/chat/send", {"csrf": self._csrf(), "message": "帮我总结一下最近的邮件"}, cookie=self.cookie)
        self.assertIn("Codex guest is not available", fallen)
        self.assertNotIn("Route: codex", fallen)

    def test_a_codex_write_shows_an_approval_card(self) -> None:
        class _Writer:
            orchestrator_ready = True

            def run(self, plane, request, on_status):
                del plane
                on_status({"phase": "planning", "backend": "stub"})
                on_status({"phase": "tool", "name": "tasks.add", "kind": "write", "backend": "stub"})
                result = request.tools("tasks.add", {"title": "Call the harbor desk"})
                return {"text": f"I asked to save the task. [{result['id']}]", "isolated": True, "boot_ms": 1, "backend": "stub", "checks": {}}

        self.plane.chat_guest = _Writer()
        status, page, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": self._csrf(), "message": "read the last 20 messages and add a task for the harbor desk"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, page)
        self.assertIn("badge write", page)
        self.assertIn("Waiting for your approval", page)
        self.assertIn('action="/ui/approvals/', page)
        self.assertNotIn("Call the harbor desk", page.split("Waiting for your approval", 1)[0])

    def test_cancel_stops_the_guest_turn(self) -> None:
        import threading
        import time

        class _Slow:
            orchestrator_ready = True
            started = threading.Event()

            def run(self, plane, request, on_status):
                del plane
                on_status({"phase": "planning", "backend": "stub"})
                self.started.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if request.cancel is not None and request.cancel.is_set():
                        raise SsiosError("cancelled", "The turn was cancelled.", 400)
                    time.sleep(0.02)
                return {"text": "should have stopped", "isolated": True, "boot_ms": 1, "backend": "stub", "checks": {}}

        self.plane.chat_guest = _Slow()
        box = {}

        def send() -> None:
            box["result"] = _form(
                self.base,
                "/ui/chat/send",
                {"csrf": self._csrf(), "message": "总结一下邮件"},
                cookie=self.cookie,
            )

        thread = threading.Thread(target=send)
        thread.start()
        self.assertTrue(self.plane.chat_guest.started.wait(2))
        status, body, _cookie = _form(self.base, "/ui/chat/cancel", {"csrf": self._csrf(), "conversation": ""}, cookie=self.cookie)
        self.assertEqual(status, 200, body)
        thread.join(4)
        self.assertIn("Cancelled", box["result"][1])
        self.assertNotIn("should have stopped", box["result"][1])

    def test_a_turn_stops_at_the_configured_time_limit(self) -> None:
        import time
        from dataclasses import replace

        class _Slow:
            orchestrator_ready = True

            def run(self, plane, request, on_status):
                del plane
                on_status({"phase": "planning", "backend": "stub"})
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if request.cancel is not None and request.cancel.is_set():
                        return {"text": "stopped", "isolated": True, "boot_ms": 1, "backend": "stub", "checks": {}}
                    time.sleep(0.02)
                return {"text": "too late", "isolated": True, "boot_ms": 1, "backend": "stub", "checks": {}}

        self.plane.settings = replace(self.plane.settings, codex_turn_timeout_seconds=0.05)
        self.plane.chat_guest = _Slow()
        status, page, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": self._csrf(), "message": "帮我总结一下最近的邮件"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, page)
        self.assertIn("time limit", page)
        self.assertNotIn("too late", page)


class GuestFailureTest(PlaneTest):
    """The Mac transcript: a ready-looking guest, then a launch failure, then the host."""

    def setUp(self) -> None:
        super().setUp()
        import ssios.chat_guest as guests

        self._report = guests.chat_guest_report
        self.guests = guests
        self.plane.accounts.add_admin("owner", PASSWORD)
        status, page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": PASSWORD})
        self.assertEqual(status, 200, page)
        self.cookie = cookie.split(";", 1)[0]
        self.account_id = next(item["id"] for item in self.plane.accounts.public_list() if item["username"] == "owner")
        self.plane.skills.set_enabled(True)
        self.plane.skills.resolver = lambda host: [__import__("ipaddress").ip_address("1.1.1.1")]
        self.plane.skills.opener = self._open
        self.addCleanup(self._restore_report)

    def _restore_report(self) -> None:
        self.guests.chat_guest_report = self._report

    def _csrf(self) -> str:
        _status, page = _get(self.base, "/ui/chat", self.cookie)
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def _send(self, message: str, conversation: str = "") -> str:
        body = {"csrf": self._csrf(), "message": message}
        if conversation:
            body["conversation"] = conversation
        status, page, _cookie = _form(self.base, "/ui/chat/send", body, cookie=self.cookie)
        self.assertEqual(status, 200, page)
        return page

    def _conversation(self, page: str) -> str:
        return page.split('class="conversation">', 1)[1].split("<", 1)[0]

    def _open(self, url, headers=None):
        del headers
        if "geocoding-api.open-meteo.com" in url:
            body = json.dumps(
                {"results": [{"name": "Bellevue", "latitude": 47.6, "longitude": -122.2, "admin1": "Washington", "country": "United States"}]}
            ).encode()
            return 200, {}, body
        if "api.open-meteo.com" in url:
            body = json.dumps({"current": {"temperature_2m": 12, "weather_code": 1, "wind_speed_10m": 10}}).encode()
            return 200, {}, body
        return 500, {}, b""

    def test_a_bare_place_in_any_script_fills_the_weather_slot(self) -> None:
        from ssios.pending_slot import consume, fresh

        slot = fresh("weather", "location", origin="今天天气怎么样", now=1_000)
        for name in ("Bellevue", "東京", "Москва"):
            filled = consume(slot, name, now=1_010)
            self.assertEqual(filled["action"], "resume", name)
            self.assertEqual(filled["value"], name)
            self.assertEqual(filled["skill"], "weather")

    def test_weather_stays_on_the_skill_and_a_bare_city_resumes_it(self) -> None:
        from ssios.chat_guest import ChatGuestRequest, ProductionChatGuest
        from ssios.orchestrator import guest_ready

        self.guests.chat_guest_report = lambda settings: {"ok": True, "detail": "files are present"}
        self.assertIsInstance(self.plane.chat_guest, ProductionChatGuest)
        self.assertTrue(guest_ready(self.plane))
        launched = []
        real_run = ProductionChatGuest.run

        def _run(guest, plane, request, on_status):
            launched.append(request.prompt)
            return real_run(guest, plane, request, on_status)

        self.plane.chat_guest.run = _run
        page = self._send("今天天气怎么样")
        self.assertIn("Route: skill", page)
        self.assertNotIn("Route: codex", page)
        self.assertIn("Which city?", page)
        self.assertNotIn("AttributeError", page)
        self.assertNotIn("chat_empty", page)
        self.assertEqual(launched, [])
        conversation = self._conversation(page)
        resumed = self._send("Bellevue", conversation)
        self.assertIn("12°C", resumed)
        self.assertIn("Bellevue", resumed)
        self.assertNotIn("Route: codex", resumed)
        self.assertNotIn("AttributeError", resumed)
        self.assertNotIn("无法获取实时", resumed)
        self.assertEqual(launched, [])
        request = ChatGuestRequest(
            account_id=self.account_id,
            account_home=self.plane.accounts.home(self.account_id),
            task_id="task-0011223344556677",
            prompt="今天天气怎么样",
        )
        with self.assertRaises(SsiosError) as caught:
            real_run(ProductionChatGuest(), self.plane, request, lambda payload: None)
        self.assertEqual(caught.exception.code, "codex_not_prepared")
        self.assertNotIsInstance(caught.exception, AttributeError)

    def test_a_failed_guest_launch_finishes_the_host_mail_summary(self) -> None:
        from ssios.chat_guest import vm_diagnose
        from ssios.macos_vz import boot_vfkit
        from ssios.orchestrator import guest_ready
        from ssios.runtime import MacOSVZRuntime
        from ssios.vmguest import GuestSession

        self.guests.chat_guest_report = lambda settings: {"ok": True, "detail": "files are present"}
        self.assertTrue(guest_ready(self.plane))
        self.plane.chat_guest = None
        self.assertFalse(guest_ready(self.plane))
        from ssios.google_connector import GoogleConnector, GoogleEndpoints
        from ssios.secret_store import MemorySecretStore

        self.plane.chat_guest = self.guests.ProductionChatGuest()
        self.plane._google = GoogleConnector(self.plane, MemorySecretStore(), GoogleEndpoints())
        self.plane.llm.report_script = ["Harbor Desk said the studio plan is on the desk. [1]"]

        def read_now(session, *, project_id, tool, arguments, limits, peer_ip="127.0.0.1"):
            del session, limits, peer_ip
            row = {"from": "Harbor Desk", "subject": "Studio plan", "date": "Nov 10", "snippet": "The studio plan is on the desk."}
            return {
                "tool": tool,
                "text": "Note || mail-header " + json.dumps(row),
                "arguments": arguments,
                "item_count": 1,
                "project_id": project_id,
            }

        self.plane.google.read_now = read_now
        page = self._send("帮我总结一下最近的邮件")
        self.assertIn("Harbor Desk said the studio plan is on the desk.", page)
        self.assertIn("Run ssios vm-diagnose for the details.", page)
        self.assertNotIn("AttributeError", page)
        self.assertNotIn("The guest stopped because of", page)
        self.assertNotIn("Assistant is writing", page)
        tasks = json.loads((self.state / "accounts" / self.account_id / "tasks.json").read_text(encoding="utf-8"))
        detail = " ".join(str(item.get("error_detail") or "") for item in tasks["tasks"].values())
        self.assertIn("codex_not_prepared", detail)
        self.assertNotIn("ya29.", detail)
        report = vm_diagnose(self.plane.settings)
        self.assertIn("codex_not_prepared", json.dumps(report))
        self.assertNotIn("ya29.", json.dumps(report))
        root = self.state / "missing-guest"
        runtime = MacOSVZRuntime(self.plane.settings)
        runtime.binary = lambda: None
        outcome = boot_vfkit(
            runtime,
            GuestSession(root=root, plain=False, grant={}, task_id="task-1", kind="chat", project_id="chat"),
            "",
            None,
            0,
            root / "kernel",
            root / "initrd",
            None,
        )
        self.assertFalse(outcome.isolated)
        self.assertIn("vfkit", outcome.detail.casefold())

    def test_an_attribute_error_inside_the_launch_is_diagnosed_not_shown(self) -> None:
        class _Broken:
            orchestrator_ready = True

            def run(self, plane, request, on_status):
                del plane, request, on_status
                raise AttributeError("socket has no attribute AF_VSOCK")

        from ssios.google_connector import GoogleConnector, GoogleEndpoints
        from ssios.secret_store import MemorySecretStore

        self.plane.chat_guest = _Broken()
        self.plane._google = GoogleConnector(self.plane, MemorySecretStore(), GoogleEndpoints())
        self.plane.llm.report_script = ["Harbor Desk said the studio plan is on the desk. [1]"]

        def read_now(session, *, project_id, tool, arguments, limits, peer_ip="127.0.0.1"):
            del session, limits, peer_ip
            row = {"from": "Harbor Desk", "subject": "Studio plan", "date": "Nov 10", "snippet": "The studio plan is on the desk."}
            return {"tool": tool, "text": "Note || mail-header " + json.dumps(row), "arguments": arguments, "item_count": 1, "project_id": project_id}

        self.plane.google.read_now = read_now
        page = self._send("帮我总结一下最近的邮件")
        self.assertIn("Harbor Desk said the studio plan is on the desk.", page)
        self.assertIn("Run ssios vm-diagnose for the details.", page)
        self.assertNotIn("AF_VSOCK", page)
        self.assertNotIn("AttributeError", page)
        tasks = json.loads((self.state / "accounts" / self.account_id / "tasks.json").read_text(encoding="utf-8"))
        detail = " ".join(str(item.get("error_detail") or "") for item in tasks["tasks"].values())
        self.assertIn("AttributeError: socket has no attribute AF_VSOCK", detail)

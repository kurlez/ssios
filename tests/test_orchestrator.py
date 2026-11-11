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
            "今天天气怎么样",
            "What's the weather in Seattle?",
            "check my inbox",
            "update the readme",
            "Summarize the harbor notes",
        )
        for phrase in codex:
            ready, note = choose_route(phrase, guest_ready=True)
            self.assertEqual(ready.route, "codex", phrase)
            self.assertEqual(note, "")
            fallen, why = choose_route(phrase, guest_ready=False)
            self.assertNotEqual(fallen.route, "codex", phrase)
            self.assertIn("Codex guest is not available", why)
        pending, _note = choose_route("我在Bellevue", guest_ready=True, pending_fill=True)
        self.assertEqual(pending.route, "codex")

    def test_citations_drop_unknown_markers_and_keep_the_text(self) -> None:
        text = present_answer("Ada wrote. [w1] Invented. [z9]", [{"id": "w1", "tool": "weather"}])
        self.assertIn("Ada wrote. [w1]", text)
        self.assertNotIn("[z9]", text)
        self.assertIn("Invented.", text)
        self.assertIn("Sources", text)
        self.assertIn("[w1] weather", text)


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
        status, page, _cookie = _form(self.base, "/ui/chat/send", {"csrf": self._csrf(), "message": "今天天气怎么样"}, cookie=self.cookie)
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
        status, fallen, _cookie = _form(self.base, "/ui/chat/send", {"csrf": self._csrf(), "message": "今天天气怎么样"}, cookie=self.cookie)
        self.assertEqual(status, 200, fallen)
        self.assertIn("Codex guest is not available", fallen)
        self.assertIn("Which city?", fallen)
        self.assertNotIn("Route: codex", fallen)

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
            {"csrf": self._csrf(), "message": "今天天气怎么样"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, page)
        self.assertIn("time limit", page)
        self.assertNotIn("too late", page)

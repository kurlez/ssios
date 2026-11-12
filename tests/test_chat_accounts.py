"""Chat routing, household isolation, LAN checks, admin console, and the connector adapter."""

from __future__ import annotations

import json
import ssl
import urllib.request

from ssios.chat_guest import ProductionChatGuest, guest_root, readiness
from ssios.chat_router import classify
import subprocess
import threading
import urllib.parse
from http.server import ThreadingHTTPServer
from unittest import mock

from ssios.google_connector import GoogleConnector, GoogleEndpoints, member_session
from ssios.secret_store import MemorySecretStore, service_name
from test_google_connector import _Google


def _event(event_id: str, title: str, start: str, end: str) -> dict:
    return {
        "id": event_id,
        "summary": title,
        "description": "",
        "location": "",
        "start": {"dateTime": start, "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end, "timeZone": "America/Los_Angeles"},
    }


class _CalendarApi(_Google):
    """Local Google stand-in that also answers Calendar."""

    writes: list[dict] = []
    mismatch = False
    listed = None

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/calendar/"):
            self._calendar("GET", path)
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/calendar/"):
            self._calendar("POST", path)
            return
        super().do_POST()

    def do_PUT(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        self._calendar("PUT", path)

    def do_DELETE(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        self._calendar("DELETE", path)

    def _calendar(self, method: str, path: str) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw.decode("utf-8")) if raw else {}
        self.writes.append({"method": method, "path": path, "body": body})
        self.seen.append({"method": method, "path": path, "query": {}, "auth": self.headers.get("Authorization", "")})
        if method == "GET" and path.endswith("/calendarList"):
            payload = {"items": [{"id": "primary", "summary": "Primary"}]}
        elif method == "GET" and "/events/" in path:
            event_id = path.rsplit("/", 1)[-1]
            listed = [item for item in (_CalendarApi.listed or []) if item.get("id") == event_id]
            if listed:
                payload = listed[0]
            else:
                title = "Other" if self.mismatch else "Standup"
                payload = {
                    "id": event_id,
                    "summary": title,
                    "start": {"dateTime": "2026-11-12T15:00:00Z", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-11-12T15:30:00Z", "timeZone": "UTC"},
                }
        elif method == "GET":
            if _CalendarApi.listed is not None:
                payload = {"items": _CalendarApi.listed}
            else:
                payload = {"items": [{"id": "ev0123456789abcd", "summary": "Standup", "start": {"dateTime": "2026-11-12T15:00:00Z"}, "end": {"dateTime": "2026-11-12T15:30:00Z"}}]}
        elif method == "DELETE":
            self.send_response(204)
            self.end_headers()
            return
        else:
            payload = {"id": body.get("id") or "ev0123456789abcd", "summary": body.get("summary") or ""}
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
from ssios.errors import SsiosError
from ssios.netguard import is_private_source, peer_allowed
from ssios.security_audit import SecurityAudit
from support import PlaneTest
from test_ui import _form, _get

PASSWORD = "correct-horse-battery"
OTHER = "other-horse-stable"


class RecordingGuest:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run(self, plane, request, on_status) -> dict:
        del plane
        guest_root(request.account_home, request.task_id)
        self.prompts.append(request.prompt)
        on_status({"phase": "booting", "backend": "fake"})
        on_status({"phase": "running", "backend": "fake"})
        checks = {"nic": ["lo"], "outbound": False, "marker": True, "sandbox_denied": True, "outside_write": False}
        on_status({"phase": "isolation", "backend": "fake", "checks": checks})
        text = "Synthetic guest result. Nothing was applied."
        if "<script>" in request.prompt:
            text = "<script>alert(1)</script>"
        return {"text": text, "isolated": True, "boot_ms": 5, "backend": "fake", "checks": checks}


class RouterTest(PlaneTest):
    def test_mail_and_drive_plans_do_not_copy_the_sentence_into_the_query(self) -> None:
        from ssios.drive_intent import plan_drive
        from ssios.mail_intent import plan_mail

        mail = plan_mail("查一下我的Gmail邮箱里有哪些发件人发给我的邮件")
        self.assertEqual(mail["q"], "in:inbox")
        self.assertEqual(mail["view"], "senders")
        self.assertNotIn("发件人", mail["q"])
        week = plan_mail("unread mail from the last 7 days")
        self.assertEqual(week["q"], "in:inbox is:unread newer_than:7d")
        self.assertEqual(plan_mail("今天的邮件")["q"], "in:inbox newer_than:1d")
        root = plan_drive("列一下我的Google Drive的根目录下的文件名")
        self.assertEqual(root, {"mode": "root"})
        named = plan_drive("list files in the Harbor folder on Google Drive")
        self.assertEqual(named, {"mode": "folder_name", "folder_name": "Harbor"})
        self.assertEqual(plan_drive("recent files in Google Drive")["mode"], "recent")
        self.assertEqual(plan_drive("search Google Drive for harbor"), {"mode": "name", "keyword": "harbor"})
        with self.assertRaises(SsiosError) as caught:
            plan_drive("list my Google Drive")
        self.assertEqual(caught.exception.code, "drive_intent_invalid")

    def test_calendar_phrases_survive_a_broken_model(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from ssios.calendar_intent import plan_calendar

        class Broken:
            def __init__(self, replies: list[str]) -> None:
                self.replies = list(replies)

            def extract_calendar_intent(self, message: str, deadline: float, repair: bool = False):
                del message, deadline, repair
                if not self.replies:
                    return "{"
                return self.replies.pop(0)

        zone = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 11, 10, 9, 0, tzinfo=zone)
        broken = Broken(["{", "not-json"])
        tomorrow, arguments, followup = plan_calendar(
            "查一下我明天有什么日程",
            now=now,
            zone_name="America/Los_Angeles",
            llm=broken,
            deadline=10**12,
        )
        self.assertEqual(tomorrow, "calendar.list_events")
        self.assertIsNone(followup)
        self.assertEqual(arguments["calendar_id"], "primary")
        self.assertEqual(arguments["time_min"], "2026-11-11T00:00:00-08:00")
        self.assertEqual(arguments["time_max"], "2026-11-12T00:00:00-08:00")
        self.assertEqual(broken.replies, ["{", "not-json"])
        week, week_args, _follow = plan_calendar(
            "what's on my calendar this week",
            now=now,
            zone_name="America/Los_Angeles",
            llm=Broken(["{", "{"]),
            deadline=10**12,
        )
        self.assertEqual(week, "calendar.list_events")
        self.assertEqual(week_args["time_min"], "2026-11-09T00:00:00-08:00")
        self.assertEqual(week_args["time_max"], "2026-11-16T00:00:00-08:00")
        monday, monday_args, _follow = plan_calendar(
            "下周一上午有什么会",
            now=now,
            zone_name="America/Los_Angeles",
            llm=Broken(["{", "{"]),
            deadline=10**12,
        )
        self.assertEqual(monday, "calendar.list_events")
        self.assertEqual(monday_args["time_min"], "2026-11-16T08:00:00-08:00")
        self.assertEqual(monday_args["time_max"], "2026-11-16T12:00:00-08:00")
        repaired = Broken(["{", '{"op":"list","range_kind":"unspecified","weekday":"","weekday_scope":"","hour":-1,"minute":0,"duration_minutes":0,"part_of_day":"","title":"","match_text":"","location":"","attendee_names":[],"attendee_emails":[],"notify":"none","calendar_id":"primary"}'])
        tool, args, _follow = plan_calendar(
            "把日历上的那件事处理一下",
            now=now,
            zone_name="America/Los_Angeles",
            llm=repaired,
            deadline=10**12,
        )
        self.assertEqual(tool, "calendar.list_events")
        self.assertEqual(args["time_min"], "2026-11-10T00:00:00-08:00")
        self.assertEqual(args["time_max"], "2026-11-17T00:00:00-08:00")
        self.assertEqual(repaired.replies, [])

    def test_each_calendar_query_keeps_its_own_range(self) -> None:
        from ssios.chat import read_blocks

        empty = read_blocks(
            "calendar.list_events",
            {"time_min": "2026-11-11T00:00:00-08:00", "time_max": "2026-11-12T00:00:00-08:00"},
            "",
            "America/Los_Angeles",
        )
        self.assertEqual(empty[0]["summary"], "Nov 11, 2026 — no events")
        self.assertEqual(empty[0]["events"], [])
        text = (
            "ev0123456789abcd Harbor sync || event-snapshot "
            '{"id":"ev0123456789abcd","title":"Harbor sync","description":"Notes https://example.com/harbor-sync",'
            '"location":"Studio B","start":"2026-11-14T18:30:00-08:00","end":"2026-11-14T20:00:00-08:00",'
            '"time_zone":"America/Los_Angeles","attendees":[]}'
        )
        filled = read_blocks(
            "calendar.list_events",
            {"time_min": "2026-11-14T00:00:00-08:00", "time_max": "2026-11-15T00:00:00-08:00"},
            text,
            "America/Los_Angeles",
        )
        card = filled[0]["events"][0]
        self.assertEqual(filled[0]["summary"], "1 event. Nov 14, 2026")
        self.assertEqual(card["title"], "Harbor sync")
        self.assertEqual(card["location"], "Studio B")
        self.assertEqual(card["when"], "Sat Nov 14, 6:30 – 8:00 PM PST")
        self.assertNotIn("ev0123456789abcd", card["title"] + card["when"] + card["location"] + card["description"])
        self.assertNotIn("id", card)

    def test_rules_override_and_default_vm(self) -> None:
        direct = classify("What is a microVM?")
        self.assertEqual(direct.route, "direct")
        self.assertEqual(direct.source, "rule")
        vm = classify("edit readme.md")
        self.assertEqual(vm.route, "vm")
        self.assertEqual(vm.source, "rule")
        plain = classify("take care of the project")
        self.assertEqual((plain.route, plain.source), ("direct", "rule"))
        taskish = classify("update the readme")
        self.assertEqual((taskish.route, taskish.source), ("vm", "default"))
        forced = classify("hello", "force_vm")
        self.assertEqual((forced.route, forced.source), ("vm", "override"))
        stayed = classify("edit readme.md", "force_direct")
        self.assertEqual(stayed.route, "direct")
        calendar = classify("list my calendars")
        self.assertEqual(calendar.route, "connector")
        self.assertEqual(calendar.services, ("calendar",))
        self.assertEqual(classify("what is a calendar?").route, "direct")
        for phrase in (
            "这周我有什么安排",
            "明天下午有空吗",
            "下周三下午三点和张三开会一小时",
            "把周五的牙医改到下午四点",
            "取消明天的站会",
            "What's on my calendar next week?",
        ):
            routed = classify(phrase)
            self.assertEqual(routed.route, "connector", phrase)
            self.assertEqual(routed.services, ("calendar",), phrase)
        self.assertEqual(classify("edit readme.md").route, "vm")
        questions = (
            "你是谁？你会什么？",
            "你是谁",
            "什么是微虚拟机？",
            "法国的首都在哪里",
            "Who are you?",
            "What can you do?",
            "What is the capital of France?",
        )
        for phrase in questions:
            routed = classify(phrase)
            self.assertEqual(routed.route, "direct", phrase)
            self.assertEqual(routed.source, "rule", phrase)
        self.assertEqual(classify("帮我修改 readme.md").route, "vm")
        self.assertEqual(classify("运行 pytest").route, "vm")

        class Spy:
            provider = "ollama"

            def __init__(self) -> None:
                self.called = False

            def judge_route(self, text, deadline=None):
                del text, deadline
                self.called = True
                return "vm"

        spy = Spy()
        self.assertEqual(classify("你是谁？你会什么？", llm=spy).route, "direct")
        self.assertFalse(spy.called)
        judged_task = classify("update the readme", llm=spy)
        self.assertTrue(spy.called)
        self.assertEqual(judged_task.route, "vm")
        self.assertEqual(judged_task.source, "model")
        mail = classify("check my inbox")
        self.assertEqual(mail.route, "connector")
        self.assertEqual(mail.services, ("gmail",))
        both = classify("save my inbox into readme.md")
        self.assertEqual(both.route, "connector_vm")

        class Local:
            provider = "ollama"

            def judge_route(self, text, deadline=None):
                del text, deadline
                return "direct"

        judged = classify("update the readme", llm=Local())
        self.assertEqual((judged.route, judged.source), ("direct", "model"))

        class Unclear:
            provider = "ollama"

            def judge_route(self, text, deadline=None):
                del text, deadline
                return "maybe"

        fallback = classify("update the readme", llm=Unclear())
        self.assertEqual(fallback.route, "vm")
        self.assertEqual(fallback.source, "model")


class LaneTest(PlaneTest):
    def test_allowlist_rejects_public_addresses(self) -> None:
        for addr in ("8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"):
            self.assertFalse(peer_allowed(addr, lan=True), addr)
            self.assertFalse(is_private_source(addr))
        for addr in ("10.1.2.3", "172.16.4.5", "192.168.1.20", "169.254.1.1", "fd00::1", "fe80::1"):
            self.assertTrue(peer_allowed(addr, lan=True), addr)
        self.assertTrue(peer_allowed("127.0.0.1", lan=False))
        self.assertFalse(peer_allowed("192.168.1.20", lan=False))
        self.assertTrue(peer_allowed("::ffff:127.0.0.1", lan=False))

    def test_public_peer_is_rejected_by_the_handler(self) -> None:
        from io import BytesIO

        from ssios.httpapi import AppHandler

        handler = AppHandler.__new__(AppHandler)
        handler.client_address = ("8.8.8.8", 9)
        handler.headers = {"Host": "127.0.0.1:9"}
        handler.path = "/ui/login"
        handler.command = "GET"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET /ui/login HTTP/1.1"
        handler.server = self.server.httpd
        handler.wfile = BytesIO()
        handler.rfile = BytesIO()
        handler.close_connection = False
        handler._dispatch()
        raw = handler.wfile.getvalue()
        self.assertIn(b"403", raw)
        self.assertIn(b"peer_rejected", raw)

    def test_routes_require_login_including_the_event_stream(self) -> None:
        for path in ("/ui", "/ui/chat", "/ui/chat/events", "/ui/connections", "/ui/admin", "/ui/audit", "/v1/health"):
            request = urllib.request.Request(self.base + path, method="GET")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 401, path)
        status, _page, _cookie = _form(self.base, "/ui/chat/send", {"message": "hello"})
        self.assertEqual(status, 401)

    def test_lan_host_and_origin_rules(self) -> None:
        from ssios.netguard import host_allowed, origin_allowed

        self.assertTrue(host_allowed("127.0.0.1:8765", lan=False))
        self.assertFalse(host_allowed("evil.example", lan=False))
        self.assertFalse(host_allowed("8.8.8.8", lan=True))
        self.assertTrue(host_allowed("192.168.1.20:8765", lan=True))
        self.assertTrue(origin_allowed("https://192.168.1.20:8765", lan=True, port=8765))
        self.assertFalse(origin_allowed("http://192.168.1.20:8765", lan=True, port=8765))
        self.assertFalse(origin_allowed("https://evil.example:8765", lan=True, port=8765))

    def test_browser_root_goes_to_login_or_chat_and_api_stays_json(self) -> None:
        status, headers, body = self._raw("GET", "/", {"Accept": "text/html"})
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/ui/login")
        self.assertNotIn(b"authentication required", body)
        status, _headers, body = self._raw("GET", "/", {"Accept": "application/json"})
        self.assertEqual(status, 401)
        self.assertIn(b"authentication required", body)
        status, _headers, body = self._raw("GET", "/", {})
        self.assertEqual(status, 401)
        self.assertIn(b"authentication required", body)
        status, headers, body = self._raw("GET", "/v1/health", {"Accept": "text/html"})
        self.assertEqual(status, 401)
        self.assertNotIn("Location", headers)
        self.assertIn(b"authentication required", body)
        status, _headers, body = self._raw(
            "GET",
            "/",
            {"Accept": "text/html", "Origin": "https://evil.example:9"},
        )
        self.assertEqual(status, 403)
        self.assertIn(b"cors_denied", body)
        self.plane.accounts.add_member("ada", "other-horse-stable")
        _status, _page, cookie = _form(
            self.base,
            "/ui/login",
            {"username": "ada", "password": "other-horse-stable"},
        )
        cookie = cookie.split(";", 1)[0]
        status, headers, body = self._raw("GET", "/", {"Accept": "text/html", "Cookie": cookie})
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/ui/chat")
        self.assertNotIn(b"authentication required", body)
        status, headers, _body = self._raw("GET", "/ui/login", {"Accept": "text/html", "Cookie": cookie})
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/ui/chat")

    def test_lan_root_redirect_keeps_peer_host_and_api_checks(self) -> None:
        from dataclasses import replace

        from ssios.httpapi import AppHandler

        self.plane.settings = replace(self.plane.settings, ui_bind="0.0.0.0")
        status, headers, body = self._dispatch(AppHandler, "/", "192.168.1.20", "192.168.1.20:8765", {"Accept": "text/html"})
        self.assertEqual(status, 302, body)
        self.assertEqual(headers.get("Location"), "/ui/login")
        self.assertNotIn(b"authentication required", body)
        status, headers, body = self._dispatch(AppHandler, "/", "8.8.8.8", "192.168.1.20:8765", {"Accept": "text/html"})
        self.assertEqual(status, 403, body)
        self.assertIn(b"peer_rejected", body)
        self.assertNotIn("Location", headers)
        status, _headers, body = self._dispatch(AppHandler, "/", "192.168.1.20", "evil.example:8765", {"Accept": "text/html"})
        self.assertEqual(status, 403, body)
        self.assertIn(b"bad_host", body)
        status, headers, body = self._dispatch(
            AppHandler,
            "/v1/health",
            "192.168.1.20",
            "192.168.1.20:8765",
            {"Accept": "text/html"},
        )
        self.assertEqual(status, 401, body)
        self.assertIn(b"authentication required", body)
        self.assertNotIn("Location", headers)
        status, _headers, body = self._dispatch(
            AppHandler,
            "/",
            "192.168.1.20",
            "192.168.1.20:8765",
            {"Accept": "text/html", "Origin": "https://evil.example:8765"},
        )
        self.assertEqual(status, 403, body)
        self.assertIn(b"cors_denied", body)

    def test_lan_startup_urls_include_the_login_path(self) -> None:
        from unittest import mock

        from ssios.netguard import browser_login_urls

        addresses = [
            (None, None, None, None, ("192.168.1.50", 0)),
            (None, None, None, None, ("8.8.8.8", 0)),
        ]
        with mock.patch("ssios.netguard.socket.gethostname", return_value="mac-mini-2"), mock.patch(
            "ssios.netguard.socket.getaddrinfo",
            return_value=addresses,
        ):
            urls = browser_login_urls(8765, tls=True)
            plain = browser_login_urls(8765, tls=False)
        self.assertEqual(urls[0], "https://127.0.0.1:8765/ui/login")
        self.assertIn("https://mac-mini-2.local:8765/ui/login", urls)
        self.assertIn("https://192.168.1.50:8765/ui/login", urls)
        self.assertNotIn("https://8.8.8.8:8765/ui/login", urls)
        self.assertEqual(plain, ["http://127.0.0.1:8765"])

    def _raw(self, method: str, path: str, headers: dict) -> tuple[int, dict, bytes]:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.plane.bound_port, timeout=5)
        merged = {"Host": f"127.0.0.1:{self.plane.bound_port}"}
        merged.update(headers)
        conn.request(method, path, headers=merged)
        response = conn.getresponse()
        payload = response.read()
        found = {key: value for key, value in response.getheaders()}
        conn.close()
        return response.status, found, payload

    def _dispatch(self, handler_type, path: str, peer: str, host: str, headers: dict) -> tuple[int, dict, bytes]:
        from io import BytesIO

        handler = handler_type.__new__(handler_type)
        handler.client_address = (peer, 9)
        handler.headers = {"Host": host, **headers}
        handler.path = path
        handler.command = "GET"
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"GET {path} HTTP/1.1"
        handler.server = self.server.httpd
        handler.wfile = BytesIO()
        handler.rfile = BytesIO()
        handler.close_connection = False
        handler._dispatch()
        raw = handler.wfile.getvalue()
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        found = {}
        for line in head.split(b"\r\n")[1:]:
            if b":" in line:
                key, value = line.split(b":", 1)
                found[key.decode()] = value.strip().decode()
        return status, found, body


class HouseholdTest(PlaneTest):
    settings_overrides = {"connectors_enabled": ("drive", "gmail", "calendar"), "connector_max_calls": 8, "connector_max_bytes": 8000}

    def setUp(self) -> None:
        super().setUp()
        _CalendarApi.seen = []
        _CalendarApi.token_reply = None
        _CalendarApi.identity_reply = None
        _CalendarApi.identity_by_path = None
        _CalendarApi.writes = []
        _CalendarApi.mismatch = False
        _CalendarApi.listed = None
        _CalendarApi.drive_files = None
        _CalendarApi.drive_folders = None
        _CalendarApi.mail_messages = None
        _CalendarApi.mail_by_id = None
        _CalendarApi.gmail_error = None
        self.google_http = ThreadingHTTPServer(("127.0.0.1", 0), _CalendarApi)
        self.addCleanup(self.google_http.server_close)
        thread = threading.Thread(target=self.google_http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.google_http.shutdown)
        port = self.google_http.server_address[1]
        base = f"http://127.0.0.1:{port}"
        self.store = MemorySecretStore()
        self.secret = "client-secret-fixture"
        self.plane._google = GoogleConnector(
            self.plane,
            self.store,
            GoogleEndpoints(
                auth_url=base + "/auth",
                token_url=base + "/token",
                userinfo_url=base + "/userinfo",
                revoke_url=base + "/revoke",
                api_base=base,
            ),
        )
        self.guest = RecordingGuest()
        self.plane.chat_guest = self.guest
        self.plane.accounts.add_admin("owner", PASSWORD)
        self.owner = self._login("owner", PASSWORD)
        self.plane.accounts.add_member("ada", OTHER)
        self.ada = self._login("ada", OTHER)

    def _login(self, username: str, password: str) -> str:
        status, page, cookie = _form(self.base, "/ui/login", {"username": username, "password": password})
        self.assertEqual(status, 200, page)
        self.assertNotIn(password, page)
        self.assertNotIn("scrypt$", page)
        return cookie.split(";", 1)[0]

    def _csrf(self, cookie: str) -> str:
        _status, page = _get(self.base, "/ui/chat", cookie)
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def _send(self, cookie: str, message: str, override: str = "auto", conversation: str = "") -> str:
        payload = {"csrf": self._csrf(cookie), "message": message, "override": override}
        if conversation:
            payload["conversation"] = conversation
        status, page, _cookie = _form(
            self.base,
            "/ui/chat/send",
            payload,
            cookie=cookie,
        )
        self.assertEqual(status, 200, page)
        return page

    def test_pure_date_questions_use_the_host_clock(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        self._los_angeles()
        calls = []
        real = self.plane.llm.stream_chat

        def wrapped(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        self.plane.llm.stream_chat = wrapped
        page = self._send(self.ada, "今天的日期")
        self.assertIn("Tuesday, 10 November 2026", page)
        self.assertIn("2026-11-10T09:00:00-08:00", page)
        self.assertIn("America/Los_Angeles", page)
        self.assertNotIn("2023", page)
        self.assertEqual(calls, [])
        tomorrow = self._send(self.ada, "明天几号")
        self.assertIn("Wednesday, 11 November 2026", tomorrow)
        self.assertIn("2026-11-11T09:00:00-08:00", tomorrow)
        weekday = self._send(self.ada, "今天星期几")
        self.assertIn("Today is Tuesday", weekday)
        clock = self._send(self.ada, "what time is it")
        self.assertIn("09:00", clock)
        self.assertIn("2026-11-10T09:00:00-08:00", clock)
        self.assertEqual(calls, [])
        self.plane.accounts.set_time_zone(self._ada_id(), "America/New_York")
        eastern = self._send(self.ada, "what time is it")
        self.assertIn("12:00", eastern)
        self.assertIn("-05:00", eastern)
        self.assertIn("America/New_York", eastern)
        self.plane.accounts.set_time_zone(self._ada_id(), "")
        self._send(self.ada, "edit readme.md")
        self.assertTrue(self.guest.prompts)
        self.assertIn("HOST-CLOCK", self.guest.prompts[-1])
        self.assertIn("2026-11-10T09:00:00-08:00", self.guest.prompts[-1])
        self.assertIn("Never invent a date", self.guest.prompts[-1])
        del datetime, ZoneInfo

    def test_direct_answer_and_vm_approval_card(self) -> None:
        direct = self._send(self.owner, "What is a microVM?")
        self.assertIn("Route: direct", direct)
        self.assertIn("Synthetic local answer. No tools were used.", direct)
        self.assertEqual(self.guest.prompts, [])
        vm = self._send(self.owner, "edit readme.md")
        self.assertIn("Route: vm", vm)
        self.assertIn("Guest: booting", vm)
        self.assertIn("sandbox_denied", vm)
        self.assertIn("Synthetic guest result. Nothing was applied.", vm)
        self.assertIn("Approve", vm)
        self.assertEqual(len(self.guest.prompts), 1)
        accepted = list((self.state / "accounts").glob("acct-*/accepted/*.md"))
        self.assertEqual(accepted, [])
        csrf = vm.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        approval = vm.split("/ui/approvals/", 1)[1].split('"', 1)[0]
        payload = vm.split('name="payload_sha256" value="', 1)[1].split('"', 1)[0]
        revision = vm.split('name="expected_revision" value="', 1)[1].split('"', 1)[0]
        bound = vm.split('name="bound_hash" value="', 1)[1].split('"', 1)[0]
        action = vm.split('name="action" value="', 1)[1].split('"', 1)[0]
        status, done, _cookie = _form(
            self.base,
            "/ui/approvals/" + approval,
            {
                "csrf": csrf,
                "decision": "approve",
                "action": action,
                "payload_sha256": payload,
                "expected_revision": revision,
                "bound_hash": bound,
            },
            cookie=self.owner,
        )
        self.assertEqual(status, 200, done)
        self.assertIn("consumed", done)
        accepted = list((self.state / "accounts").glob("acct-*/accepted/*.md"))
        self.assertEqual(len(accepted), 1)

    def test_stream_requires_csrf_and_escapes_guest_text(self) -> None:
        csrf = self._csrf(self.owner)
        status, page, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": "wrong", "message": "edit readme.md"},
            cookie=self.owner,
            extra={"Accept": "text/event-stream"},
        )
        self.assertEqual(status, 403, page)
        status, page, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": csrf, "message": "edit readme.md"},
            cookie=self.owner,
            extra={"Accept": "text/event-stream"},
        )
        self.assertEqual(status, 200, page)
        self.assertIn("event: route", page)
        self.assertIn("event: status", page)
        escaped = self._send(self.owner, "<script>edit</script> readme.md")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", escaped)
        self.assertNotIn("<script>alert(1)</script>", escaped)

    def test_accounts_cannot_read_each_other(self) -> None:
        phrase = "MEMBER-ONLY-MARKER"
        self._send(self.ada, f"hello {phrase}")
        _status, owner_chat = _get(self.base, "/ui/chat", self.owner)
        self.assertNotIn(phrase, owner_chat)
        conv = (self.state / "accounts").glob("acct-*/chat/conv-*.json")
        ada_conv = ""
        for path in conv:
            text = path.read_text(encoding="utf-8")
            if phrase in text:
                ada_conv = json.loads(text)["id"]
                break
        self.assertTrue(ada_conv)
        _status, peeked = _get(self.base, f"/ui/chat?conversation={ada_conv}", self.owner)
        self.assertNotIn(phrase, peeked)
        _status, traversed = _get(self.base, "/ui/chat?conversation=../../secrets/human.token", self.ada)
        self.assertNotIn(self.human, traversed)
        _status, memory = _get(self.base, "/ui/memory?project_id=demo-alpha&path=project.md", self.ada)
        self.assertNotIn("CORE-PROJECT-FACT", memory)
        request = urllib.request.Request(
            self.base + "/ui/memory?path=../../vault/canary.txt",
            headers={"Cookie": self.ada},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        body = caught.exception.read().decode()
        self.assertNotIn("canary", body.casefold())
        code, task, _headers = self.call(
            "POST",
            "/v1/tasks",
            token=self.human,
            body={"project_id": "demo-alpha", "kind": "project-summary", "input_revision": "exp-1", "input_path": "notes.md"},
        )
        self.assertEqual(code, 202, task)
        _status, hidden = _get(self.base, f"/ui/tasks/{task['id']}", self.ada)
        self.assertIn("Task not found", hidden)
        self.assertNotIn("demo-alpha", hidden)

    def test_connector_approval_before_fetch_and_admin_cannot_see_content(self) -> None:
        csrf = self._csrf(self.ada)
        status, page, _cookie = _form(
            self.base,
            "/ui/connections",
            {"csrf": csrf, "service": "gmail", "op": "connect", "account_id": self.plane.accounts.legacy_admin()["id"]},
            cookie=self.ada,
        )
        self.assertEqual(status, 403, page)
        self._install_and_connect(self._ada_id(), "gmail", "code-a")
        self.assertEqual(service_name(self._ada_id()), f"ssios.google.{self._ada_id()}")
        seen_before = len(_CalendarApi.seen)
        pending = self._send(self.ada, "check my inbox")
        self.assertNotIn('value="connector.fetch"', pending)
        self.assertNotIn("No access to send or modify", pending)
        self.assertIn("Fixture subject", pending)
        self.assertIn("Harbor Bot", pending)
        self.assertIn("Inbox, latest 50", pending)
        self.assertTrue(any(item["path"].startswith("/gmail/") for item in _CalendarApi.seen[seen_before:]))
        self.assertEqual(self.guest.prompts, [])
        self.assertTrue(any(item["path"] == "/gmail/v1/users/me/messages" for item in _CalendarApi.seen))
        self.assertEqual(self.guest.prompts, [])
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(self.secret, audit)
        self.assertNotIn("refresh-account-a", audit)
        self.assertNotIn("ya29.", audit)
        self.assertNotIn("check my inbox", audit)
        _status, admin = _get(self.base, "/ui/admin", self.owner)
        self.assertIn("gmail yes", admin)
        self.assertIn("calendar no", admin)
        self.assertNotIn(self.secret, admin)
        self.assertNotIn("refresh-account-a", admin)
        self.assertNotIn("Fixture subject", admin)
        status, stolen, _cookie = _form(
            self.base,
            "/ui/connections",
            {"csrf": self._csrf(self.owner), "service": "gmail", "op": "connect", "account_id": self._ada_id()},
            cookie=self.owner,
        )
        self.assertEqual(status, 403, stolen)

    def test_admin_registration_reset_delete_and_audit(self) -> None:
        status, pending, _cookie = _form(self.base, "/ui/signup", {"username": "beau", "password": "beau-password-ok", "role": "admin"})
        self.assertEqual(status, 403, pending)
        status, requested, _cookie = _form(self.base, "/ui/signup", {"username": "beau", "password": "beau-password-ok"})
        self.assertEqual(status, 200, requested)
        denied, _page, _cookie = _form(self.base, "/ui/login", {"username": "beau", "password": "beau-password-ok"})
        self.assertEqual(denied, 403)
        beau = self._account_named("beau")
        status, approved, _cookie = _form(
            self.base,
            f"/ui/admin/users/{beau}",
            {"csrf": self._csrf(self.owner), "op": "approve"},
            cookie=self.owner,
        )
        self.assertEqual(status, 200, approved)
        beau_cookie = self._login("beau", "beau-password-ok")
        status, blocked, _cookie = _form(
            self.base,
            f"/ui/admin/users/{beau}",
            {"csrf": self._csrf(beau_cookie), "op": "role-admin"},
            cookie=beau_cookie,
        )
        self.assertEqual(status, 403, blocked)
        self.assertEqual(self._role(beau), "member")
        self._install_and_connect(beau, "drive", "code-b")
        self.assertEqual(self.store.get(beau, "drive-refresh"), "refresh-account-b")
        status, reset, _cookie = _form(
            self.base,
            f"/ui/admin/users/{beau}",
            {"csrf": self._csrf(self.owner), "op": "reset-password", "password": "beau-password-new"},
            cookie=self.owner,
        )
        self.assertEqual(status, 200, reset)
        self.assertNotIn("beau-password-new", reset)
        self.assertNotIn(self.secret, reset)
        self.assertNotIn("refresh-account-b", reset)
        self.assertIsNone(self.store.get(beau, "drive-refresh"))
        old = urllib.request.Request(self.base + "/ui/chat", headers={"Cookie": beau_cookie})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(old, timeout=5)
        self.assertEqual(caught.exception.code, 401)
        self._login("beau", "beau-password-new")
        self._send(self.ada, "hello from ada")
        status, deleted, _cookie = _form(
            self.base,
            f"/ui/admin/users/{self._ada_id()}",
            {"csrf": self._csrf(self.owner), "op": "delete", "confirm": "delete ada"},
            cookie=self.owner,
        )
        self.assertEqual(status, 200, deleted)
        self.assertIsNone(self.store.get(self._ada_id(), "gmail-refresh"))
        self.assertTrue((self.state / "accounts" / "archive").is_dir())
        self.assertNotIn("hello from ada", deleted)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(self.base + "/ui/chat", headers={"Cookie": self.ada}), timeout=5)
        report = self.plane.security_audit.verify()
        self.assertTrue(report["ok"], report)
        raw = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(PASSWORD, raw)
        self.assertNotIn("beau-password", raw)
        self.assertNotIn("scrypt$", raw)
        tampered = raw.replace("login", "login-tampered", 1)
        path = self.state / "audit" / "security.jsonl"
        path.write_text(tampered, encoding="utf-8")
        self.assertFalse(SecurityAudit(path).verify()["ok"])

    def test_signup_member_and_admin_own_connections(self) -> None:
        created = self.plane.accounts.signup("kent", "kent-password-ok")
        self.assertEqual(created["role"], "member")
        self.assertFalse(created["legacy_owner"])
        _status, pending_page = _get(self.base, "/ui/admin", self.owner)
        forms = pending_page.split(f'action="/ui/admin/users/{created["id"]}"')[1:]
        kent_ops = " ".join(part.split("</form>", 1)[0] for part in forms)
        self.assertIn("approve", kent_ops)
        self.assertIn("reject", kent_ops)
        self.assertNotIn("role-admin", kent_ops)
        self.assertNotIn("Reset password", kent_ops)
        self.assertIn("New password", pending_page)
        self.plane.accounts.set_status(created["id"], "active")
        self.assertEqual(self.plane.accounts.get(created["id"])["role"], "member")
        owner_id = self.plane.accounts.legacy_admin()["id"]
        owner_ops = " ".join(part.split("</form>", 1)[0] for part in pending_page.split(f'action="/ui/admin/users/{owner_id}"')[1:])
        self.assertIn("Make member", owner_ops)
        self.assertNotIn("Make admin", owner_ops)
        self.assertIn("Disable", owner_ops)
        status, blocked, _cookie = _form(
            self.base,
            f"/ui/admin/users/{owner_id}",
            {"csrf": self._csrf(self.owner), "op": "role-member"},
            cookie=self.owner,
        )
        self.assertEqual(status, 403, blocked)
        self.assertIn("own role", blocked)
        status, blocked, _cookie = _form(
            self.base,
            f"/ui/admin/users/{owner_id}",
            {"csrf": self._csrf(self.owner), "op": "delete", "confirm": "delete owner"},
            cookie=self.owner,
        )
        self.assertEqual(status, 403, blocked)
        self.assertEqual(self._role(owner_id), "admin")
        from ssios.ui_admin import _guard_admin_target

        with self.assertRaises(SsiosError) as caught:
            _guard_admin_target(self.plane, created["id"], self.plane.accounts.get(owner_id), "delete")
        self.assertIn("last active admin", caught.exception.detail)
        kent = self._login("kent", "kent-password-ok")
        _status, connections = _get(self.base, "/ui/connections", kent)
        self.assertIn("Signed in as kent", connections)
        self.assertNotIn(">Admin</a>", connections)
        self.assertIn("<h2>Gmail</h2><p>Not connected</p>", connections)
        self.assertNotIn("not connected / not connected", connections)
        self.assertIn("Connect gmail", connections)
        self.assertIn("Connect drive", connections)
        self.assertIn("Connect calendar", connections)
        self.assertNotIn("Disconnect gmail", connections)
        self._install_and_connect(created["id"], "gmail", "code-a")
        _status, connected = _get(self.base, "/ui/connections", kent)
        self.assertIn("Disconnect gmail", connected)
        self.assertNotIn("Connect gmail", connected)
        self.assertIn("Connect drive", connected)
        self.store.put(owner_id, "client-id", "client-owner")
        self.store.put(owner_id, "client-secret", self.secret)
        status, started, _cookie = _form(
            self.base,
            "/ui/connections",
            {"csrf": self._csrf(self.owner), "service": "calendar", "op": "connect"},
            cookie=self.owner,
        )
        self.assertEqual(status, 200, started)
        self.assertIn("Open Google consent", started)
        self.assertIn("readonly", started)
        self.assertNotIn("admin cannot access", started)
        self.assertIn("Connect calendar", started)
        from ssios.ui_household import _connections_page

        remote = _connections_page(self.plane, {"account": self.plane.accounts.get(created["id"]), "csrf": "csrf-token"}, "", "192.168.1.20")
        self.assertIn("not on the Mac", remote)
        self.assertIn("Finish consent", remote)
        self.assertIn("redirect_url", remote)
        status, page, replaced = _form(
            self.base,
            "/ui/login",
            {"username": "kent", "password": "kent-password-ok"},
            cookie=self.owner,
        )
        self.assertEqual(status, 200, page)
        self.assertIn("Signed in as kent", page)
        self.assertIsNone(self.plane.accounts.read_session(self.owner.split("=", 1)[1]))
        both = self.owner + "; " + replaced.split(";", 1)[0]
        _status, chat = _get(self.base, "/ui/chat", both)
        self.assertIn("Signed in as kent", chat)
        self.assertNotIn("Signed in as owner", chat)

    def test_owner_admin_uses_own_connections_and_ui_errors_are_html(self) -> None:
        status, content_type, body = self._open("/ui/connections", self.owner, "text/html")
        self.assertEqual(status, 200, body)
        self.assertIn("text/html", content_type)
        text = body.decode()
        self.assertIn("Signed in as owner", text)
        self.assertIn("Connect gmail", text)
        self.assertIn("Connect drive", text)
        self.assertIn("Connect calendar", text)
        self.assertNotIn("connector_admin_denied", text)
        self.assertFalse(text.startswith("{"))
        for path in ("/ui/chat", "/ui/memory?project_id=local-summary"):
            status, content_type, page = self._open(path, self.owner, "text/html")
            self.assertEqual(status, 200, page)
            self.assertIn("text/html", content_type)
            self.assertIn("Signed in as owner", page.decode())
        status, content_type, denied = self._open("/ui/admin", self.ada, "text/html")
        self.assertEqual(status, 403, denied)
        self.assertIn("text/html", content_type)
        self.assertIn(b"Something went wrong", denied)
        self.assertIn(b"admin role required", denied)
        self.assertNotIn(b'{"error"', denied)
        status, content_type, raw = self._open("/ui/admin", self.ada, "application/json")
        self.assertEqual(status, 403, raw)
        self.assertTrue(raw.startswith(b"{"), raw)
        self.assertIn(b"admin role required", raw)
        self._install_and_connect(self._ada_id(), "gmail", "code-a")
        listed = self._send(self.ada, "check my inbox")
        self.assertNotIn('value="connector.fetch"', listed)
        self._install_and_connect(self._ada_id(), "calendar", "code-a")
        self._los_angeles()
        pending = self._send(self.ada, "下周三下午三点和张三开会一小时")
        status, _type, hidden = self._open("/ui/approvals", self.owner, "text/html")
        self.assertEqual(status, 200, hidden)
        self.assertNotIn("张三", hidden.decode())
        approval = pending.split('action="/ui/approvals/', 1)[1].split('"', 1)[0]
        status, page, _cookie = _form(
            self.base,
            "/ui/approvals/" + approval,
            {**self._binding(self.owner, pending, "approve")},
            cookie=self.owner,
            extra={"Accept": "text/html"},
        )
        self.assertEqual(status, 403, page)
        self.assertIn("another account", page)
        self.assertNotIn('{"error"', page)
        self.assertIn("Something went wrong", page)
        owner_id = self.plane.accounts.legacy_admin()["id"]
        self.store.put(owner_id, "client-id", "client-owner")
        self.store.put(owner_id, "client-secret", self.secret)
        status, started, _cookie = _form(
            self.base,
            "/ui/connections",
            {"csrf": self._csrf(self.owner), "service": "drive", "op": "connect"},
            cookie=self.owner,
            extra={"Accept": "text/html"},
        )
        self.assertEqual(status, 200, started)
        self.assertIn("Open Google consent", started)
        self.assertNotIn("connector_admin_denied", started)
        self.assertIsNone(self.store.get(self._ada_id(), "drive-refresh"))
        self.assertNotEqual(service_name(owner_id), service_name(self._ada_id()))

    def test_approved_cloud_text_reaches_the_guest_only_after_approval(self) -> None:
        self._install_and_connect(self._ada_id(), "gmail", "code-a")
        pending = self._send(self.ada, "save my inbox into readme.md")
        self.assertIn("Route: connector_vm", pending)
        self.assertNotIn("connector.fetch", pending)
        self.assertTrue(any(item["path"] == "/gmail/v1/users/me/messages" for item in _CalendarApi.seen))
        self.assertEqual(len(self.guest.prompts), 1)
        self.assertIn("UNTRUSTED EXTERNAL CONTENT", self.guest.prompts[0])
        self.assertIn("Fixture subject", self.guest.prompts[0])
        self.assertNotIn("msg-1", self.guest.prompts[0])
        self.assertIn("Synthetic guest result", pending)
        self.assertIn("Nothing was applied", pending)

    def test_ui_flows_do_not_put_secrets_on_subprocess_argv(self) -> None:
        argv = []

        def record(args) -> None:
            if isinstance(args, (list, tuple)):
                argv.append([str(part) for part in args])

        real_run = subprocess.run
        real_popen = subprocess.Popen

        def spy_run(args, *pos, **kwargs):
            record(args)
            return real_run(args, *pos, **kwargs)

        def spy_popen(args, *pos, **kwargs):
            record(args)
            return real_popen(args, *pos, **kwargs)

        with mock.patch("subprocess.run", spy_run), mock.patch("subprocess.Popen", spy_popen):
            csrf = self._csrf(self.ada)
            status, connected, _cookie = _form(
                self.base,
                "/ui/connections",
                {"csrf": csrf, "service": "gmail", "op": "connect"},
                cookie=self.ada,
            )
            self.assertEqual(status, 400, connected)
            self._install_and_connect(self._ada_id(), "gmail", "code-a")
            status, connected, _cookie = _form(
                self.base,
                "/ui/connections",
                {"csrf": self._csrf(self.ada), "service": "drive", "op": "connect"},
                cookie=self.ada,
            )
            self.assertEqual(status, 200, connected)
            self.assertNotIn(self.secret, connected)
            pending = self._send(self.ada, "check my inbox")
            self.assertIn("Fixture subject", pending)
            self.assertNotIn('value="connector.fetch"', pending)
            status, reset, _cookie = _form(
                self.base,
                f"/ui/admin/users/{self._ada_id()}",
                {"csrf": self._csrf(self.owner), "op": "reset-password", "password": "ada-password-new1"},
                cookie=self.owner,
            )
            self.assertEqual(status, 200, reset)
            self.plane.accounts.add_member("cara", "cara-password-ok")
            cara = self._account_named("cara")
            self._install_and_connect(cara, "gmail", "code-b")
            status, deleted, _cookie = _form(
                self.base,
                f"/ui/admin/users/{cara}",
                {"csrf": self._csrf(self.owner), "op": "delete", "confirm": "delete cara"},
                cookie=self.owner,
            )
            self.assertEqual(status, 200, deleted)
        blob = json.dumps(argv)
        for secret in (self.secret, "refresh-account-a", "refresh-account-b", "ya29.access-account-a", "ada-password-new1"):
            self.assertNotIn(secret, blob)

    def test_memory_is_not_written_until_the_owner_approves(self) -> None:
        pending = self._send(self.ada, "remember the fixture code is HARBOR")
        self.assertIn("Route: memory", pending)
        self.assertIn("Nothing is written until you approve", pending)
        _status, before = _get(self.base, "/ui/memory", self.ada)
        self.assertNotIn("HARBOR", before)
        self._approve(self.ada, pending)
        _status, listing = _get(self.base, "/ui/memory", self.ada)
        self.assertIn("topics/chat/", listing)
        self.assertNotIn("topics/chat/note.md", listing)
        import re as _re
        found = _re.search(r'href="/ui/memory\?path=(topics/chat/[^"]+)"', listing)
        self.assertIsNotNone(found)
        _status, after = _get(self.base, "/ui/memory?path=" + found.group(1), self.ada)
        self.assertIn("HARBOR", after)
        self.assertIn("History", after)
        request = urllib.request.Request(
            self.base + "/ui/memory?project_id=local-summary&path=" + found.group(1),
            headers={"Cookie": self.owner},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                owner = response.read().decode()
        except urllib.error.HTTPError as exc:
            owner = exc.read().decode()
        self.assertNotIn("HARBOR", owner)
        again = self._approve(self.ada, pending)
        self.assertIn("Approved", again)
        self.assertIn("Saved to memory", again)
        self.assertIn("topics/chat/", again)
        self.assertIn("Back to the conversation", again)
        self.assertNotIn("no longer pending", again)

    def _approve(self, cookie: str, page: str) -> str:
        csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        approval = page.split("/ui/approvals/", 1)[1].split('"', 1)[0]
        payload = page.split('name="payload_sha256" value="', 1)[1].split('"', 1)[0]
        revision = page.split('name="expected_revision" value="', 1)[1].split('"', 1)[0]
        bound = page.split('name="bound_hash" value="', 1)[1].split('"', 1)[0]
        action = page.split('name="action" value="', 1)[1].split('"', 1)[0]
        status, done, _cookie = _form(
            self.base,
            "/ui/approvals/" + approval,
            {
                "csrf": csrf,
                "decision": "approve",
                "action": action,
                "payload_sha256": payload,
                "expected_revision": revision,
                "bound_hash": bound,
            },
            cookie=cookie,
        )
        self.assertEqual(status, 200, done)
        return done

    def test_connection_client_form_hides_the_secret_and_respects_toggles(self) -> None:
        from dataclasses import replace

        secret = "uploaded-secret-value"
        status, page, _cookie = _form(
            self.base,
            "/ui/connections",
            {"csrf": self._csrf(self.ada), "op": "install-client", "client_id": "desktop-client-abcdef", "client_secret": secret},
            cookie=self.ada,
        )
        self.assertEqual(status, 200, page)
        self.assertIn("Set up. Client id ends in abcdef", page)
        self.assertNotIn(secret, page)
        self.assertEqual(self.store.get(self._ada_id(), "client-secret"), secret)
        self.assertIsNone(self.store.get(self.plane.accounts.legacy_admin()["id"], "client-secret"))
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("oauth.client.install", audit)
        self.assertNotIn(secret, audit)
        for path in self.state.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())
        status, denied, _cookie = _form(
            self.base,
            "/ui/connections",
            {"op": "install-client", "client_id": "other", "client_secret": "other-secret"},
            cookie=self.ada,
        )
        self.assertEqual(status, 403, denied)
        status, foreign, _cookie = _form(
            self.base,
            "/ui/connections",
            {
                "csrf": self._csrf(self.ada),
                "op": "install-client",
                "account_id": self.plane.accounts.legacy_admin()["id"],
                "client_id": "owner-client",
                "client_secret": secret,
            },
            cookie=self.ada,
        )
        self.assertEqual(status, 403, foreign)
        self.assertIsNone(self.store.get(self.plane.accounts.legacy_admin()["id"], "client-id"))
        boundary = "----ssiosform"
        uploaded = json.dumps({"installed": {"client_id": "file-client-zzzzzz", "client_secret": "file-secret-value"}})
        raw = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{self._csrf(self.ada)}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"op\"\r\n\r\ninstall-client\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"client_file\"; filename=\"client_secret_test.json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{uploaded}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        request = urllib.request.Request(
            self.base + "/ui/connections",
            data=raw,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Cookie": self.ada},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            uploaded_page = response.read().decode()
        self.assertIn("ends in zzzzzz", uploaded_page)
        self.assertNotIn("file-secret-value", uploaded_page)
        self.assertIn("oauth.client.replace", (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(self.store.get(self._ada_id(), "client-secret"), "file-secret-value")
        for path in self.state.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"file-secret-value", path.read_bytes())
        self.plane.settings = replace(self.plane.settings, connectors_enabled=("gmail",))
        gated = self._open("/ui/connections", self.ada, "text/html")[2].decode()
        self.assertIn("Disabled in config by the administrator.", gated)
        status, blocked, _cookie = _form(
            self.base,
            "/ui/connections",
            {"csrf": self._csrf(self.ada), "op": "enable", "service": "calendar"},
            cookie=self.ada,
        )
        self.assertEqual(status, 403, blocked)
        status, off, _cookie = _form(
            self.base,
            "/ui/connections",
            {"csrf": self._csrf(self.ada), "op": "disable", "service": "gmail"},
            cookie=self.ada,
        )
        self.assertEqual(status, 200, off)
        self.assertIn("Turn on this service to connect it.", off)
        self.assertIn("connector.disable", (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8"))
        status, kept, _cookie = _form(
            self.base,
            "/ui/connections",
            {"csrf": self._csrf(self.ada), "op": "remove-client", "confirm": "no"},
            cookie=self.ada,
        )
        self.assertEqual(status, 400, kept)
        self.assertEqual(self.store.get(self._ada_id(), "client-secret"), "file-secret-value")
        status, removed, _cookie = _form(
            self.base,
            "/ui/connections",
            {"csrf": self._csrf(self.ada), "op": "remove-client", "confirm": "remove"},
            cookie=self.ada,
        )
        self.assertEqual(status, 200, removed)
        self.assertIn("Not set up", removed)
        self.assertIsNone(self.store.get(self._ada_id(), "client-secret"))
        self.assertIn("oauth.client.remove", (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8"))
        self.plane.settings = replace(self.plane.settings, connectors_enabled=("drive", "gmail", "calendar"))

    def test_connections_scroll_and_put_consent_on_the_service_card(self) -> None:
        from ssios.ui import APP_CSS
        from ssios.ui_household import _connections_page

        self.assertIn("body.app-body { overflow: hidden; }", APP_CSS)
        self.assertIn(".stage { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }", APP_CSS)
        self.assertIn(".page { flex: 1; min-height: 0; overflow-y: auto;", APP_CSS)
        self.assertIn(".log { flex: 1; overflow: auto; min-height: 0; }", APP_CSS)
        page = _connections_page(
            self.plane,
            {"account": self.plane.accounts.get(self._ada_id()), "csrf": "csrf-token"},
            "",
            "192.168.1.20",
        )
        self.assertIn('class="page"', page)
        self.assertIn('class="sheet"', page)
        self.assertIn("https://console.cloud.google.com/apis/library/calendar-json.googleapis.com", page)
        self.assertIn("https://console.cloud.google.com/apis/library/gmail.googleapis.com", page)
        self.assertIn("https://console.cloud.google.com/apis/library/drive.googleapis.com", page)
        self.assertNotIn("<select", page)
        self.store.put(self._ada_id(), "client-id", "desktop-client-abcdef.apps.googleusercontent.com")
        self.store.put(self._ada_id(), "client-secret", "GOCSPX-test")
        ready = _connections_page(
            self.plane,
            {"account": self.plane.accounts.get(self._ada_id()), "csrf": "csrf-token"},
            "",
            "192.168.1.20",
        )
        self.assertIn("Manage client", ready)
        self.assertIn("<details", ready)
        self.assertIn("not on the Mac", ready)
        self.assertIn("Finish consent", ready)
        self.assertIn('name="redirect_url"', ready)
        self.assertIn('name="op" value="finish"', ready)
        self.assertIn("enterkeyhint=\"done\"", ready)
        focused = _connections_page(
            self.plane,
            {"account": self.plane.accounts.get(self._ada_id()), "csrf": "csrf-token"},
            "<p class=\"notice\">The consent code expired. Click Connect again.</p>",
            "192.168.1.20",
            focus="gmail",
        )
        client = focused.split('<article class="svc"', 1)[0]
        gmail = focused.split("<h2>Gmail</h2>", 1)[1].split("<h2>Drive</h2>", 1)[0]
        self.assertNotIn("code expired", client)
        self.assertIn("code expired", gmail)
        from dataclasses import replace

        self.plane.settings = replace(self.plane.settings, connectors_enabled=("gmail",))
        gated = _connections_page(
            self.plane,
            {"account": self.plane.accounts.get(self._ada_id()), "csrf": "csrf-token"},
            "",
            "127.0.0.1",
        )
        from pathlib import Path

        enabled_line = next(
            index
            for index, line in enumerate(Path(self.plane.settings.config_path).read_text(encoding="utf-8").splitlines(), start=1)
            if line.strip() == "enabled = []"
        )
        self.assertIn(f"line {enabled_line}", gated)
        self.assertIn("ssios.example.toml", gated)
        self.assertIn("add &quot;calendar&quot; to enabled", gated)
        self.plane.settings = replace(self.plane.settings, connectors_enabled=("drive", "gmail", "calendar"))

    def test_desktop_json_connects_and_keychain_errors_stay_on_the_card(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO

        from ssios.secret_store import KeyringSecretStore

        secret = "GOCSPX-desktop-secret-value"
        client_id = "1234567890-abc.apps.googleusercontent.com"
        uploaded = json.dumps(
            {
                "installed": {
                    "client_id": client_id,
                    "project_id": "ssios-local",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "client_secret": secret,
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
        boundary = "----ssiosdesktop"
        raw = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{self._csrf(self.ada)}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"op\"\r\n\r\ninstall-client\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"client_file\"; filename=\"client_secret_desktop.json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{uploaded}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        request = urllib.request.Request(
            self.base + "/ui/connections",
            data=raw,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Cookie": self.ada, "Accept": "text/html"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read().decode()
        self.assertIn("ends in", page)
        self.assertNotIn(secret, page)
        self.assertEqual(self.store.get(self._ada_id(), "client-secret"), secret)
        for path in self.state.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())
        for service in ("gmail", "drive", "calendar"):
            status, connected, _cookie = _form(
                self.base,
                "/ui/connections",
                {"csrf": self._csrf(self.ada), "op": "connect", "service": service},
                cookie=self.ada,
                extra={"Accept": "text/html"},
            )
            self.assertEqual(status, 200, connected)
            self.assertIn("127.0.0.1", connected)
            self.assertIn(client_id, connected)
            self.assertNotIn(secret, connected)
            self.assertNotIn("internal error", connected)
        web = json.dumps({"web": {"client_id": "web-client-id", "client_secret": secret}})
        web_raw = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{self._csrf(self.ada)}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"op\"\r\n\r\ninstall-client\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"client_file\"; filename=\"client_secret_web.json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{web}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        web_request = urllib.request.Request(
            self.base + "/ui/connections",
            data=web_raw,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Cookie": self.ada, "Accept": "text/html"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(web_request, timeout=5) as response:
                web_status, web_page = response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            web_status, web_page = exc.code, exc.read().decode()
        self.assertEqual(web_status, 400, web_page)
        self.assertIn("Desktop client", web_page)
        self.assertIn("Google OAuth client", web_page)
        self.assertNotIn(secret, web_page)

        class Locked:
            def set_password(self, service, username, password):
                raise RuntimeError("Can't store password on keychain: (-25308, 'Unknown Error')")

            def get_password(self, service, username):
                raise RuntimeError("Can't get password on keychain: (-25308, 'Unknown Error')")

            def delete_password(self, service, username):
                raise RuntimeError("Can't delete password on keychain: (-25308, 'Unknown Error')")

        self.plane.google.store = KeyringSecretStore(Locked())
        stderr = StringIO()
        with redirect_stderr(stderr):
            status, locked, _cookie = _form(
                self.base,
                "/ui/connections",
                {"csrf": self._csrf(self.ada), "op": "install-client", "client_id": client_id, "client_secret": secret},
                cookie=self.ada,
                extra={"Accept": "text/html"},
            )
        self.assertEqual(status, 503, locked)
        self.assertIn("keychain_locked", locked)
        self.assertIn("unlock-keychain", locked)
        self.assertIn("Google OAuth client", locked)
        self.assertNotIn(secret, locked)
        self.assertNotIn("internal error", locked)
        logged = stderr.getvalue()
        self.assertIn("class=RuntimeError", logged)
        self.assertIn("-25308", logged)
        self.assertNotIn(secret, logged)
        self.plane.google.store = self.store

        def boom(*_args, **_kwargs):
            raise RuntimeError("boom " + secret)

        self.plane.google.connect = boom
        stderr = StringIO()
        with redirect_stderr(stderr):
            status, broken, _cookie = _form(
                self.base,
                "/ui/connections",
                {"csrf": self._csrf(self.ada), "op": "connect", "service": "gmail"},
                cookie=self.ada,
                extra={"Accept": "text/html"},
            )
        self.assertEqual(status, 500, broken)
        self.assertIn("Error code: internal", broken)
        self.assertIn("Google OAuth client", broken)
        self.assertNotIn(secret, broken)
        self.assertNotIn("internal error", broken)
        self.assertIn("class=RuntimeError", stderr.getvalue())
        self.assertNotIn(secret, stderr.getvalue())
        stderr = StringIO()
        with redirect_stderr(stderr):
            status, raw, _cookie = _form(
                self.base,
                "/ui/connections",
                {"csrf": self._csrf(self.ada), "op": "connect", "service": "drive"},
                cookie=self.ada,
            )
        self.assertEqual(status, 500, raw)
        self.assertIn('"error": "internal"', raw)
        self.assertIn("Error code: internal", raw)
        self.assertNotIn('"detail": "internal error"', raw)
        self.assertNotIn(secret, raw)
        self.assertNotIn(secret, stderr.getvalue())

    def test_calendar_connections_approvals_and_errors(self) -> None:
        from ssios.chat import english_connector_error
        from ssios.errors import SsiosError

        self.assertIn("not requested", english_connector_error(SsiosError("oauth_scope_widened", "raw", 409)))
        self.assertIn("31 days", english_connector_error(SsiosError("connector_window_denied", "raw", 400)))
        self.assertIn("was not sent", english_connector_error(SsiosError("connector_conflict", "raw", 409)))
        self._install_and_connect(self._ada_id(), "calendar", "code-a")
        _status, connections = _get(self.base, "/ui/connections", self.ada)
        self.assertIn("<h2>Calendar</h2><p>Connected</p>", connections)
        self.assertIn("<h2>Gmail</h2><p>Not connected</p>", connections)
        self.assertIn("<h2>Drive</h2><p>Not connected</p>", connections)
        self.assertNotIn(self.secret, connections)
        status, window, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": self._csrf(self.ada), "message": "list calendar events from:2026-01-01T00:00:00Z to:2026-03-01T00:00:00Z"},
            cookie=self.ada,
        )
        self.assertEqual(status, 400, window)
        self.assertIn("cannot be longer than 31 days", window)
        self.assertEqual(_CalendarApi.writes, [])
        created = self._send(
            self.ada,
            "create calendar event title:Standup start:2026-11-12T15:00:00Z end:2026-11-12T15:30:00Z zone:UTC calendar:primary send:none",
        )
        self.assertIn("Create event", created)
        self.assertIn("Attendees are not emailed", created)
        event_id = created.split("Event id: ", 1)[1].split("\n", 1)[0].strip()
        updated = self._send(
            self.ada,
            "update calendar event event:ev0123456789abcd calendar:primary send:none "
            "before_title:Standup before_start:2026-11-12T15:00:00Z before_end:2026-11-12T15:30:00Z before_zone:UTC "
            "after_title:Later after_start:2026-11-12T16:00:00Z after_end:2026-11-12T16:30:00Z after_zone:UTC",
        )
        self.assertIn("Before:", updated)
        self.assertIn("After:", updated)
        self.assertIn("Standup", updated)
        self.assertIn("Later", updated)
        deleted = self._send(
            self.ada,
            "delete calendar event event:ev0123456789abcd calendar:primary title:Standup "
            "start:2026-11-12T15:00:00Z end:2026-11-12T15:30:00Z zone:UTC send:none",
        )
        self.assertIn("DESTRUCTIVE", deleted)
        self.assertIn("approval danger", deleted)
        _status, inbox = _get(self.base, "/ui/approvals", self.ada)
        self.assertIn("Create event", inbox)
        self.assertIn("approval danger", inbox)
        _status, hidden = _get(self.base, "/ui/approvals", self.owner)
        self.assertNotIn("Standup", hidden)
        self.plane.accounts.add_member("bea", "bea-password-ok")
        bea = self._login("bea", "bea-password-ok")
        _status, foreign = _get(self.base, "/ui/approvals", bea)
        self.assertNotIn("Standup", foreign)
        status, blocked, _cookie = _form(
            self.base,
            "/ui/approvals/" + created.split("/ui/approvals/", 1)[1].split('"', 1)[0],
            {"csrf": self._csrf(self.owner), "decision": "approve", "action": "connector.write", "payload_sha256": "x", "expected_revision": "connector", "bound_hash": "y"},
            cookie=self.owner,
        )
        self.assertEqual(status, 403, blocked)
        delete_id = deleted.split("/ui/approvals/", 1)[1].split('"', 1)[0]
        status, denied_page, _cookie = _form(
            self.base,
            "/ui/approvals/" + delete_id,
            self._binding(self.ada, deleted, "deny"),
            cookie=self.ada,
        )
        self.assertEqual(status, 200, denied_page)
        self.assertIn("denied", denied_page)
        self.assertFalse(any(item["method"] == "DELETE" for item in _CalendarApi.writes))
        done = self._approve(self.ada, created)
        self.assertIn(f"Created event {event_id}", done)
        posts = [item for item in _CalendarApi.writes if item["method"] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["body"].get("id"), event_id)
        self.assertEqual(posts[0]["body"].get("summary"), "Standup")
        status, replay, _cookie = _form(
            self.base,
            "/ui/approvals/" + created.split("/ui/approvals/", 1)[1].split('"', 1)[0],
            self._binding(self.ada, created, "approve"),
            cookie=self.ada,
        )
        self.assertEqual(status, 409, replay)
        self.assertEqual(len([item for item in _CalendarApi.writes if item["method"] == "POST"]), 1)
        record = self.plane.approvals.get(updated.split("/ui/approvals/", 1)[1].split('"', 1)[0])
        record["expires_at"] = 0
        self.plane.approvals.save()
        status, expired, _cookie = _form(
            self.base,
            "/ui/approvals/" + record["approval_id"],
            self._binding(self.ada, updated, "approve"),
            cookie=self.ada,
        )
        self.assertIn(str(status), {"200", "409"}, expired)
        self.assertNotIn("Updated event", expired)
        self.assertFalse(any(item["method"] == "PUT" for item in _CalendarApi.writes))
        fresh = self._send(
            self.ada,
            "update calendar event event:ev0123456789abcd calendar:primary send:all "
            "before_title:Standup before_start:2026-11-12T15:00:00Z before_end:2026-11-12T15:30:00Z before_zone:UTC "
            "after_title:Later after_start:2026-11-12T16:00:00Z after_end:2026-11-12T16:30:00Z after_zone:UTC",
        )
        self.assertIn("This will email attendees", fresh)
        _CalendarApi.mismatch = True
        status, conflict, _cookie = _form(
            self.base,
            "/ui/approvals/" + fresh.split("/ui/approvals/", 1)[1].split('"', 1)[0],
            self._binding(self.ada, fresh, "approve"),
            cookie=self.ada,
        )
        self.assertEqual(status, 409, conflict)
        self.assertIn("was not sent", conflict)
        self.assertFalse(any(item["method"] == "PUT" for item in _CalendarApi.writes))
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("Standup", audit)
        self.assertNotIn(self.secret, audit)
        self.assertNotIn("refresh-account", audit)
        _status, admin = _get(self.base, "/ui/admin", self.owner)
        self.assertIn("calendar yes", admin)
        self.assertNotIn("Standup", admin)
        self.assertNotIn(self.secret, admin)

    def test_chinese_mail_and_drive_reads_list_results_without_approval(self) -> None:
        import io
        from contextlib import redirect_stderr

        self._install_and_connect(self._ada_id(), "gmail", "code-a")
        self._install_and_connect(self._ada_id(), "drive", "code-a")
        _CalendarApi.mail_messages = [{"id": "m-ada-1"}, {"id": "m-ada-2"}, {"id": "m-bea"}]
        _CalendarApi.mail_by_id = {
            "m-ada-1": [
                {"name": "From", "value": "Ada Example <ada@example.com>"},
                {"name": "Subject", "value": "Harbor notes"},
                {"name": "Date", "value": "Tue, 10 Nov 2026 18:00:00 -0800"},
            ],
            "m-ada-2": [
                {"name": "From", "value": "Ada Example <ada@example.com>"},
                {"name": "Subject", "value": "Studio plan"},
                {"name": "Date", "value": "Wed, 11 Nov 2026 09:00:00 -0800"},
            ],
            "m-bea": [
                {"name": "From", "value": "Bea Example <bea@example.com>"},
                {"name": "Subject", "value": "Schedule"},
                {"name": "Date", "value": "Wed, 11 Nov 2026 10:00:00 -0800"},
            ],
        }
        captured = io.StringIO()
        with redirect_stderr(captured):
            mail = self._send(self.ada, "查一下我的Gmail邮箱里有哪些发件人发给我的邮件")
        self.assertIn("2 senders. Inbox, latest 50", mail)
        self.assertIn("Ada Example", mail)
        self.assertIn("2 messages", mail)
        self.assertIn("Bea Example", mail)
        self.assertNotIn("No access to send", mail)
        self.assertNotIn("m-ada-1", mail)
        self.assertNotIn('value="connector.fetch"', mail)
        log = captured.getvalue()
        self.assertIn("query=in:inbox", log)
        self.assertNotIn("发件人", log)
        self.assertNotIn("ada@example.com", log)
        _CalendarApi.drive_files = [
            {
                "id": "file-harbor",
                "name": "Harbor notes.txt",
                "mimeType": "text/plain",
                "parents": ["root"],
                "modifiedTime": "2026-11-10T18:00:00Z",
                "webViewLink": "https://example.com/harbor-notes",
            },
            {"id": "file-other", "name": "Other.txt", "mimeType": "text/plain", "parents": ["elsewhere"]},
        ]
        drive = self._send(self.ada, "列一下我的Google Drive的根目录下的文件名")
        self.assertIn("1 file. My Drive", drive)
        self.assertIn("Harbor notes.txt", drive)
        self.assertIn("File", drive)
        self.assertIn("https://example.com/harbor-notes", drive)
        self.assertIn("Open", drive)
        self.assertNotIn("Other.txt", drive)
        self.assertNotIn("file-harbor", drive)
        self.assertNotIn("folder:FOLDER_ID", drive)
        self.assertNotIn("connector_grant_denied", drive)
        self.assertNotIn("vm-diagnose", drive)
        status, unclear, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": self._csrf(self.ada), "message": "list my Google Drive"},
            cookie=self.ada,
        )
        self.assertEqual(status, 400, unclear)
        self.assertIn("drive_intent_invalid", unclear)
        self.assertIn("根目录", unclear)
        self.assertNotIn("connector_grant_denied", unclear)
        self.assertNotIn("vm-diagnose", unclear)

    def test_failed_or_empty_gmail_never_invents_mail(self) -> None:
        from ssios.chat import _mentions_are_in_results

        blocks = [{"events": [{"title": "Harbor notes", "meta": "Harbor Bot <harbor@example.com>", "when": "Nov 10, 2026"}]}]
        self.assertFalse(_mentions_are_in_results("Jane Doe jane@example.com wrote Project Q1 2023", blocks))
        self.assertTrue(_mentions_are_in_results("Harbor Bot sent Harbor notes", blocks))
        self._install_and_connect(self._ada_id(), "gmail", "code-a")
        calls = []
        real = self.plane.llm.complete_chat

        def wrapped(*args, **kwargs):
            calls.append(args)
            return "No changes are made. Jane Doe <jane@example.com> sent Project Q1 2023 on January 1, 2023."

        self.plane.llm.complete_chat = wrapped
        _CalendarApi.gmail_error = {
            "status": 403,
            "body": {
                "error": {
                    "code": 403,
                    "message": "Google Gmail API has not been used in project 1 before or it is disabled.",
                    "status": "PERMISSION_DENIED",
                    "errors": [{"reason": "accessNotConfigured"}],
                }
            },
        }
        status, failed, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": self._csrf(self.ada), "message": "查一下我的邮件"},
            cookie=self.ada,
        )
        self.assertEqual(status, 403, failed)
        self.assertIn("Route: connector", failed)
        self.assertIn("Gmail returned an error", failed)
        self.assertIn("Gmail API", failed)
        self.assertIn("connector_api_disabled", failed)
        self.assertNotIn("Jane Doe", failed)
        self.assertNotIn("jane@example.com", failed)
        self.assertNotIn("2023", failed)
        self.assertNotIn("No changes are made", failed)
        self.assertEqual(calls, [])
        _CalendarApi.gmail_error = None
        _CalendarApi.mail_messages = []
        empty = self._send(self.ada, "check my inbox")
        self.assertIn("No messages matched in:inbox", empty)
        self.assertNotIn("Jane Doe", empty)
        self.assertNotIn("example.com", empty)
        self.assertEqual(calls, [])
        _CalendarApi.mail_messages = [{"id": "msg-1"}]
        _CalendarApi.mail_by_id = None
        listed = self._send(self.ada, "check my inbox")
        self.assertIn("Fixture subject", listed)
        self.assertIn("Harbor Bot", listed)
        self.assertNotIn("Jane Doe", listed)
        self.assertNotIn("2023", listed)
        self.assertEqual(len(calls), 1)
        self.plane.llm.complete_chat = real

    def test_saved_name_is_recalled_in_a_new_conversation(self) -> None:
        pending = self._send(self.ada, "My name is Harbor")
        self.assertIn("human.md", pending)
        self.assertIn("Nothing is written until you approve", pending)
        _status, before = _get(self.base, "/ui/memory", self.ada)
        self.assertNotIn("Harbor", before)
        self._approve(self.ada, pending)
        calls = []

        def wrapped(messages, cancel_event, deadline):
            calls.append(messages)
            yield "I do not know."

        self.plane.llm.stream_chat = wrapped
        again = self._send(self.ada, "what do you know about me")
        self.assertIn("Used memory: human.md", again)
        self.assertTrue(calls)
        blob = json.dumps(calls[0], ensure_ascii=False)
        self.assertIn("MEMORY-CONTEXT", blob)
        self.assertIn("Harbor", blob)
        self.assertIn("- name: Harbor", blob)
        repo = self.state / "accounts" / self._ada_id() / "memory" / "notes" / "repo"
        log = subprocess.check_output(["git", "-C", str(repo), "log", "--oneline"], text=True)
        self.assertGreaterEqual(len(log.splitlines()), 2)
        shown = subprocess.check_output(["git", "-C", str(repo), "show", "HEAD:human.md"], text=True)
        self.assertIn("Harbor", shown)
        _status, owner = _get(self.base, "/ui/memory", self.owner)
        self.assertNotIn("Harbor", owner)

    def test_reminder_uses_an_absolute_time_across_midnight(self) -> None:
        from dataclasses import replace
        from datetime import datetime
        from zoneinfo import ZoneInfo

        zone = ZoneInfo("America/Los_Angeles")
        self.plane.settings = replace(self.plane.settings, ui_time_zone="America/Los_Angeles")
        self.plane.calendar_now = datetime(2026, 11, 10, 23, 50, tzinfo=zone)
        calls = []

        def wrapped(*args, **kwargs):
            calls.append(args)
            yield "I recorded that."

        self.plane.llm.stream_chat = wrapped
        saved = self._send(self.ada, "帮我记一下，我明天上午11点需要给Chase打电话")
        self.assertIn("Route: memory", saved)
        self.assertIn("Saved: Wed Nov 11, 11:00 AM PT", saved)
        self.assertIn("2026-11-11T11:00:00-08:00", saved)
        mirror = (self.state / "accounts" / self._ada_id() / "Tasks.md").read_text(encoding="utf-8")
        self.assertIn("2026-11-11T11:00:00-08:00", mirror)
        self.assertIn("Chase", mirror)
        self.assertIn("Chase", saved)
        self.assertNotIn("I recorded that", saved)
        self.assertEqual(calls, [])
        self.plane.calendar_now = datetime(2026, 11, 11, 0, 30, tzinfo=zone)
        today = self._send(self.ada, "我今天有什么事情")
        self.assertIn("2026-11-11T11:00:00-08:00", today)
        self.assertIn("Chase", today)
        self.assertNotIn("I recorded that", today)
        tomorrow = self._send(self.ada, "我明天需要做什么")
        self.assertIn("Tomorrow — no reminders.", tomorrow)
        self.assertNotIn("Chase", tomorrow)
        self.assertIn("Calendar is not connected.", tomorrow)

    def test_chinese_name_is_saved_to_human_and_recalled(self) -> None:
        phrases = (
            "记住我叫Kent",
            "记住：我叫Kent",
            "我叫Kent，记住",
            "请记住我的名字是Kent",
            "remember my name is Kent",
        )
        first = self._send(self.ada, phrases[0])
        self.assertIn("Waiting for your approval", first)
        self.assertIn("human.md", first)
        self.assertIn("- name: Kent", first)
        self.assertIn("Nothing is written until you approve", first)
        _status, before = _get(self.base, "/ui/memory?path=human.md", self.ada)
        self.assertNotIn("Kent", before)
        self.assertIn("ssios seed account memory", before)
        approved = self._approve(self.ada, first)
        self.assertIn("Saved to memory (human.md)", approved)
        _status, panel = _get(self.base, "/ui/memory?path=human.md", self.ada)
        self.assertIn("- name: Kent", panel)
        self.assertIn("ssios approved memory", panel)
        self.assertNotIn("Kent", (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8"))
        repo = self.state / "accounts" / self._ada_id() / "memory" / "notes" / "repo"
        shown = subprocess.check_output(["git", "-C", str(repo), "show", "HEAD:human.md"], text=True)
        self.assertIn("- name: Kent", shown)
        log = subprocess.check_output(["git", "-C", str(repo), "log", "--oneline"], text=True)
        self.assertGreaterEqual(len(log.splitlines()), 2)
        for index, phrase in enumerate(phrases[1:], start=2):
            self.plane.accounts.add_member(f"ada{index}", OTHER)
            cookie = self._login(f"ada{index}", OTHER)
            page = self._send(cookie, phrase)
            self.assertIn("Waiting for your approval to save human.md", page, phrase)
            self.assertIn("- name: Kent", page)
            self._approve(cookie, page)
            _status, member_panel = _get(self.base, "/ui/memory?path=human.md", cookie)
            self.assertIn("- name: Kent", member_panel)
        asked = self._send(self.ada, "我叫什么？")
        self.assertIn("Your name is Kent.", asked)
        self.assertIn("Used memory: human.md", asked)
        from ssios.chat import _messages
        from ssios.scope import AccountScope

        scope = AccountScope(self.plane, self.plane.accounts.users[self._ada_id()])
        conversation = asked.split('class="conversation">', 1)[1].split("<", 1)[0]
        prompt = json.dumps(_messages(scope, conversation, ""), ensure_ascii=False)
        self.assertIn("MEMORY-CONTEXT", prompt)
        self.assertIn("human.md", prompt)
        self.assertIn("Kent", prompt)

    def test_expired_memory_approval_can_be_retried(self) -> None:
        pending = self._send(self.ada, "remember the fixture code is QUAY")
        self.assertIn("Nothing is written until you approve", pending)
        approval = pending.split("/ui/approvals/", 1)[1].split('"', 1)[0]
        path = self.plane.accounts.home(self._ada_id()) / "approvals.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["approvals"][approval]["expires_at"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")
        status, expired, _cookie = _form(
            self.base,
            "/ui/approvals/" + approval,
            {
                "csrf": pending.split('name="csrf" value="', 1)[1].split('"', 1)[0],
                "decision": "approve",
                "action": "memory.commit",
                "payload_sha256": pending.split('name="payload_sha256" value="', 1)[1].split('"', 1)[0],
                "expected_revision": pending.split('name="expected_revision" value="', 1)[1].split('"', 1)[0],
                "bound_hash": pending.split('name="bound_hash" value="', 1)[1].split('"', 1)[0],
            },
            cookie=self.ada,
        )
        self.assertEqual(status, 200, expired)
        self.assertIn("Expired", expired)
        self.assertIn(">Retry</button>", expired)
        _status, before = _get(self.base, "/ui/memory", self.ada)
        self.assertNotIn("QUAY", before)
        status, retried, _cookie = _form(
            self.base,
            "/ui/approvals/" + approval,
            {"csrf": self._csrf(self.ada), "decision": "retry"},
            cookie=self.ada,
        )
        self.assertEqual(status, 200, retried)
        self.assertIn("New approval", retried)
        self.assertIn("Superseded", retried)
        approved = self._approve(self.ada, retried)
        self.assertIn("Approved", approved)
        self.assertIn("Saved to memory", approved)
        import re

        _status, after = _get(self.base, "/ui/memory", self.ada)
        found = re.search(r'href="/ui/memory\?path=(topics/chat/[^"]+)"', after)
        self.assertIsNotNone(found)
        _status, note = _get(self.base, "/ui/memory?path=" + found.group(1), self.ada)
        self.assertIn("QUAY", note)

    def test_explicit_goal_shows_a_chip_and_the_tasks_page(self) -> None:
        page = self._send(self.ada, "please call the harbor desk")
        self.assertIn("Added goal: call the harbor desk", page)
        self.assertIn(">Undo</button>", page)
        _status, tasks = _get(self.base, "/ui/tasks", self.ada)
        self.assertIn("call the harbor desk", tasks)
        self.assertIn("Source", tasks)
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("goal.create", audit)
        self.assertNotIn("harbor desk", audit)

    def test_natural_language_calendar_examples_need_approval(self) -> None:
        from ssios.errors import SsiosError

        self._install_and_connect(self._ada_id(), "calendar", "code-a")
        self._los_angeles()
        _CalendarApi.writes.clear()
        week = self._send(self.ada, "这周我有什么安排")
        self.assertIn("Route: connector", week)
        self.assertNotIn("Route: vm", week)
        self.assertIn("2026-11-09T00:00:00-08:00", week)
        self.assertIn("2026-11-16T00:00:00-08:00", week)
        self.assertIn("Monday 00:00", week)
        self.assertIn("Nov 9", week)
        self.assertNotIn("event-snapshot", week)
        self.assertNotIn("ev0123456789abcd", week)
        self.assertNotIn('value="connector.fetch"', week)
        afternoon = self._send(self.ada, "明天下午有空吗")
        self.assertIn("2026-11-11T13:00:00-08:00", afternoon)
        self.assertIn("2026-11-11T17:00:00-08:00", afternoon)
        nxt = self._send(self.ada, "What's on my calendar next week?")
        self.assertIn("Route: connector", nxt)
        self.assertIn("2026-11-16T00:00:00-08:00", nxt)
        self.assertIn("2026-11-23T00:00:00-08:00", nxt)
        created = self._send(self.ada, "下周三下午三点和张三开会一小时")
        self.assertIn("2026-11-18T15:00:00-08:00", created)
        self.assertIn("2026-11-18T16:00:00-08:00", created)
        self.assertIn("America/Los_Angeles", created)
        self.assertIn("张三", created)
        self.assertIn("Attendees: (none)", created)
        self.assertIn("Attendees are not emailed", created)
        card = created.split("<pre>", 1)[1].split("</pre>", 1)[0]
        self.assertNotIn("@", card)
        self.assertFalse(any(item["method"] in {"POST", "PUT", "DELETE"} for item in _CalendarApi.writes))
        self.plane.llm.intent_script = ["nope"]
        explicit = self._send(
            self.ada,
            "create calendar event title:Standup start:2026-11-12T15:00:00Z end:2026-11-12T15:30:00Z zone:UTC calendar:primary send:none",
        )
        self.assertIn("Create event", explicit)
        self.assertIn("Standup", explicit)
        self.assertEqual(self.plane.llm.intent_script, ["nope"])
        _CalendarApi.writes.clear()
        self.plane.llm.intent_script = ["{", "{"]
        tomorrow = self._send(self.ada, "查一下我明天有什么日程")
        self.assertIn("2026-11-11T00:00:00-08:00", tomorrow)
        self.assertIn("2026-11-12T00:00:00-08:00", tomorrow)
        self.assertIn("共 1 项。", tomorrow)
        self.assertNotIn("vm-diagnose", tomorrow)
        self.assertNotIn('value="connector.fetch"', tomorrow)
        week_again = self._send(self.ada, "what's on my calendar this week")
        self.assertIn("2026-11-09T00:00:00-08:00", week_again)
        self.assertIn("2026-11-16T00:00:00-08:00", week_again)
        self.assertIn("1 items.", week_again)
        monday = self._send(self.ada, "下周一上午有什么会")
        self.assertIn("2026-11-16T08:00:00-08:00", monday)
        self.assertIn("2026-11-16T12:00:00-08:00", monday)
        self.assertIn("共 1 项。", monday)
        self.assertEqual(self.plane.llm.intent_script, ["{", "{"])
        self.plane.llm.intent_script = ["{", "{"]
        status, failed, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": self._csrf(self.ada), "message": "把日历改一下"},
            cookie=self.ada,
        )
        self.assertEqual(status, 400, failed)
        self.assertIn("calendar_intent_invalid", failed)
        self.assertIn("Nothing was read or changed", failed)
        self.assertIn("查一下我明天有什么日程", failed)
        self.assertNotIn("vm-diagnose", failed)
        self.plane.llm.intent_script = [SsiosError("llm_unavailable", "down", 503)]
        _CalendarApi.writes.clear()
        down = self._send(self.ada, "取消明天的站会")
        self.assertIn("2026-11-11T00:00:00-08:00", down)
        self.assertNotIn("vm-diagnose", down)
        self.assertFalse(any(item["method"] == "DELETE" for item in _CalendarApi.writes))

    def test_natural_language_update_and_delete_ask_before_writing(self) -> None:
        self._install_and_connect(self._ada_id(), "calendar", "code-a")
        self._los_angeles()
        _CalendarApi.listed = [
            _event("ev0123456789abcd", "牙医", "2026-11-13T09:00:00-08:00", "2026-11-13T10:00:00-08:00"),
            _event("ev0123456789abce", "牙医复查", "2026-11-13T11:00:00-08:00", "2026-11-13T12:00:00-08:00"),
        ]
        _CalendarApi.writes.clear()
        listed = self._send(self.ada, "把周五的牙医改到下午四点")
        self.assertIn("2026-11-13T00:00:00-08:00", listed)
        self.assertNotIn("Today is that weekday", listed)
        self.assertNotIn("name=\"action\" value=\"connector.fetch\"", listed)
        self.assertFalse(any(item["method"] in {"POST", "PUT", "DELETE"} for item in _CalendarApi.writes))
        self.assertIn("Several events match", listed)
        found = listed
        self.assertIn("Reply with a number", found)
        self.assertNotIn("Update event", found)
        self.assertFalse(any(item["method"] == "PUT" for item in _CalendarApi.writes))
        conversation = found.split('class="conversation">', 1)[1].split("<", 1)[0]
        chosen = self._send(self.ada, "2", conversation=conversation)
        self.assertIn("Update event", chosen)
        self.assertIn("ev0123456789abce", chosen)
        self.assertNotIn("ev0123456789abcd", chosen)
        self.assertIn("2026-11-13T16:00:00-08:00", chosen)
        self.assertIn("Attendees are not emailed", chosen)
        self.assertFalse(any(item["method"] == "PUT" for item in _CalendarApi.writes))
        saved = self._approve(self.ada, chosen)
        self.assertIn("Updated event ev0123456789abce", saved)
        puts = [item for item in _CalendarApi.writes if item["method"] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["body"].get("summary"), "牙医复查")
        self.assertEqual(puts[0]["body"].get("attendees"), [])
        _CalendarApi.listed = [
            _event("ev0123456789abcf", "站会", "2026-11-11T09:00:00-08:00", "2026-11-11T09:15:00-08:00"),
        ]
        _CalendarApi.writes.clear()
        removal = self._send(self.ada, "取消明天的站会")
        self.assertFalse(any(item["method"] == "DELETE" for item in _CalendarApi.writes))
        self.assertIn("DESTRUCTIVE", removal)
        self.assertIn("approval danger", removal)
        self.assertIn("2026-11-11T09:00:00-08:00", removal)
        self.assertIn("ev0123456789abcf", removal)
        done = self._approve(self.ada, removal)
        self.assertIn("Deleted event ev0123456789abcf", done)
        self.assertEqual(len([item for item in _CalendarApi.writes if item["method"] == "DELETE"]), 1)

    def test_vm_failure_is_saved_in_the_bubble_and_diagnose_is_redacted(self) -> None:
        from ssios.chat_guest import vm_diagnose
        from ssios.chat_store import ChatStore
        from ssios.errors import SsiosError

        class MissingBinary:
            def run(self, plane, request, on_status):
                raise SsiosError("codex_not_prepared", "pinned Codex binary is missing; run ssios vm-prepare on the host", 503)

        self.plane.chat_guest = MissingBinary()
        status, page, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": self._csrf(self.ada), "message": "edit readme.md"},
            cookie=self.ada,
        )
        self.assertEqual(status, 503, page)
        self.assertIn("Error code: codex_not_prepared", page)
        self.assertIn("ssios vm-diagnose", page)
        conv = page.split('class="conversation">', 1)[1].split("<", 1)[0]
        home = self.plane.accounts.home(self._ada_id())
        stored = json.dumps(ChatStore(home).get(conv))
        self.assertIn("Error code: codex_not_prepared", stored)
        reloaded = self._open("/ui/chat?conversation=" + conv, self.ada, "text/html")[2].decode()
        self.assertIn("Error code: codex_not_prepared", reloaded)
        tasks = json.loads((home / "tasks.json").read_text(encoding="utf-8"))["tasks"]
        self.assertTrue(any(item.get("error_code") == "codex_not_prepared" for item in tasks.values()))

        class Boom:
            def run(self, plane, request, on_status):
                raise RuntimeError("guest-0123456789abcdef secret-bubble")

        self.plane.chat_guest = Boom()
        status, crashed, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": self._csrf(self.ada), "message": "edit a file in the workspace"},
            cookie=self.ada,
        )
        self.assertEqual(status, 503, crashed)
        self.assertIn("Error code: chat_failed", crashed)
        self.assertIn("RuntimeError", crashed)
        self.assertNotIn("secret-bubble", crashed)
        self.assertNotIn("guest-0123456789abcdef", crashed)

        guest = home / "workspace" / "tasks" / "task-0011223344556677" / "guest"
        guest.mkdir(parents=True)
        (guest / "serial.log").write_text("prompt guest-0123456789abcdef\nRuntimeError: disk full\n", encoding="utf-8")
        (home / "tasks.json").write_text(
            json.dumps({"tasks": {"task-0011223344556677": {"id": "task-0011223344556677", "status": "failed", "error_code": "codex_not_prepared", "error_detail": "guest-0123456789abcdef missing"}}}),
            encoding="utf-8",
        )
        report = json.dumps(vm_diagnose(self.plane.settings))
        self.assertIn("codex_not_prepared", report)
        self.assertIn("RuntimeError: disk full", report)
        self.assertNotIn("guest-0123456789abcdef", report)
        self.assertIn("serial.log", report)
        self.assertNotIn("secret-bubble", report)

    def test_chat_history_sidebar_and_sanitized_markdown(self) -> None:
        first = self._send(self.ada, "What is a microVM?")
        self.assertIn("Route: direct", first)
        conv = first.split('class="conversation">', 1)[1].split("<", 1)[0]
        home = self.plane.accounts.home(self._ada_id())
        self.assertTrue((home / "chat" / f"{conv}.json").is_file())
        from ssios.chat_store import ChatStore
        from ssios.service import ControlPlane

        reloaded = ChatStore(home).get(conv)
        self.assertIn("What is a microVM?", json.dumps(reloaded))
        restarted = ControlPlane(self.plane.settings)
        restarted.open()
        again = restarted.accounts.public_list()
        self.assertTrue(any(item["username"] == "ada" for item in again))
        self.assertIn("What is a microVM?", json.dumps(ChatStore(home).get(conv)))
        page = self._open("/ui/chat?conversation=" + conv, self.ada, "text/html")[2].decode()
        self.assertIn("What is a microVM?", page)
        self.assertIn("New chat", page)
        self.assertIn("Rename", page)
        self.assertIn("Delete", page)
        self.assertIn("How can I help?", page)
        self.assertIn("Message SSIOS", page)
        self.assertIn("Enter to send, Shift+Enter for a new line", page)
        self.assertNotIn("<details class=\"more\"", page)
        self.assertIn("menu-label\">More", page)
        href = page.split('rel="stylesheet" href="', 1)[1].split('"', 1)[0]
        with urllib.request.urlopen(self.base + href, timeout=5) as response:
            css = response.read()
            content_type = response.headers.get("Content-Type", "")
            self.assertEqual(response.status, 200)
        self.assertTrue(content_type.startswith("text/css"))
        self.assertIn(b"text-decoration: none", css)
        self.assertIn(b"summary::-webkit-details-marker", css)
        self.assertIn(b".menu-btn { display: none", css)
        _status, owner_view = _get(self.base, "/ui/chat?conversation=" + conv, self.owner)
        self.assertNotIn("What is a microVM?", owner_view)
        status, renamed, _cookie = _form(
            self.base,
            "/ui/chat/rename",
            {"csrf": self._csrf(self.ada), "conversation": conv, "title": "Harbor notes"},
            cookie=self.ada,
        )
        self.assertIn("Harbor notes", renamed)
        self.assertEqual(ChatStore(home).get(conv)["title"], "Harbor notes")
        poisoned = "<script>alert(1)</script> [x](javascript:alert(1))"
        ChatStore(home).append(conv, {"role": "assistant", "text": poisoned})
        shown = self._open("/ui/chat?conversation=" + conv, self.ada, "text/html")[2].decode()
        self.assertNotIn("<script>alert(1)</script>", shown)
        self.assertNotIn('href="javascript:', shown)
        self.assertIn("\\u003cscript\\u003e", shown)
        guest = self._send(self.ada, "edit readme.md", conversation=conv)
        self.assertIn("Approve", guest)
        history = self._open("/ui/chat?conversation=" + conv, self.ada, "text/html")[2].decode()
        self.assertIn('"status": "pending"', history)
        self.assertIn('"actionable": true', history)
        self._approve(self.ada, guest)
        after = self._open("/ui/chat?conversation=" + conv, self.ada, "text/html")[2].decode()
        self.assertIn('"status": "approved"', after)
        self.assertIn('"actionable": false', after)
        status, deleted, _cookie = _form(
            self.base,
            "/ui/chat/delete",
            {"csrf": self._csrf(self.ada), "conversation": conv},
            cookie=self.ada,
        )
        self.assertNotIn("Harbor notes", deleted)
        self.assertFalse((home / "chat" / f"{conv}.json").is_file())

    def _open(self, path: str, cookie: str, accept: str) -> tuple[int, str, bytes]:
        request = urllib.request.Request(self.base + path, headers={"Cookie": cookie, "Accept": accept})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers.get("Content-Type", ""), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read()

    def _los_angeles(self) -> None:
        from dataclasses import replace
        from datetime import datetime
        from zoneinfo import ZoneInfo

        zone = ZoneInfo("America/Los_Angeles")
        self.plane.settings = replace(self.plane.settings, ui_time_zone="America/Los_Angeles")
        self.plane.calendar_now = datetime(2026, 11, 10, 9, 0, tzinfo=zone)

    def _binding(self, cookie: str, page: str, decision: str) -> dict:
        return {
            "csrf": self._csrf(cookie),
            "decision": decision,
            "action": page.split('name="action" value="', 1)[1].split('"', 1)[0],
            "payload_sha256": page.split('name="payload_sha256" value="', 1)[1].split('"', 1)[0],
            "expected_revision": page.split('name="expected_revision" value="', 1)[1].split('"', 1)[0],
            "bound_hash": page.split('name="bound_hash" value="', 1)[1].split('"', 1)[0],
        }

    def _install_and_connect(self, account_id: str, connector: str, code: str) -> None:
        session = member_session(account_id)
        self.plane.google.install_client(session, "client-" + account_id[:8], self.secret)
        started = self.plane.google.connect(session, connector, open_browser=False, timeout=5)
        parsed = urllib.parse.urlsplit(started["authorization_url"])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertNotIn(self.secret, started["authorization_url"])
        redirect = query["redirect_uri"][0]
        state = query["state"][0]
        urllib.request.urlopen(redirect + "?" + urllib.parse.urlencode({"code": code, "state": state}), timeout=5).read()
        self.plane.google.finish_connect(session, connector, timeout=5)

    def _ada_id(self) -> str:
        return self._account_named("ada")

    def _account_named(self, username: str) -> str:
        for account in self.plane.accounts.public_list():
            if account["username"] == username:
                return account["id"]
        self.fail(username)

    def _role(self, account_id: str) -> str:
        for account in self.plane.accounts.public_list():
            if account["id"] == account_id:
                return account["role"]
        self.fail(account_id)


class AdapterTest(PlaneTest):
    def test_sources_do_not_pass_secrets_on_a_command_line(self) -> None:
        from support import ROOT

        needles = ("add-generic-" + "password", "secret-" + "tool")
        for path in (ROOT / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                self.assertNotIn(needle, text, f"{path} contains {needle}")

    def test_guest_fails_closed_without_running_on_the_host(self) -> None:
        from dataclasses import replace
        from io import StringIO
        from contextlib import redirect_stdout

        from ssios.chat_guest import chat_guest_report
        from ssios.cli import build_parser, run_vm_prepare

        report = readiness(self.plane, "cloud-hypervisor")
        self.assertFalse(report["ok"])
        guest = ProductionChatGuest()

        class Boom:
            def run(self, *args, **kwargs):
                raise AssertionError("host agent ran")

        self.plane.runner = Boom()
        statuses = []
        request = type(
            "R",
            (),
            {
                "account_id": "acct-0011223344556677",
                "account_home": self.state / "accounts" / "acct-0011223344556677",
                "task_id": "task-0011223344556677",
                "prompt": "edit",
            },
        )()
        with self.assertRaises(SsiosError) as caught:
            guest.run(self.plane, request, statuses.append)
        self.assertEqual(caught.exception.code, "microvm_fail_closed")
        self.assertEqual(statuses, [])
        self.assertIn('backend = "dev-guard"', caught.exception.detail)
        self.assertIn('agent = "summary"', caught.exception.detail)
        self.assertIn('backend = "macos-vz"', caught.exception.detail)
        self.assertIn("vm-prepare", caught.exception.detail)
        self.assertIn("--state", caught.exception.detail)
        self.assertIn("--config", caught.exception.detail)
        self.assertIn("does not accept --profile", caught.exception.detail)
        self.assertNotIn("--profile ", caught.exception.detail.split("run: ", 1)[1])
        self.assertNotIn("krunvm", caught.exception.detail)
        self.plane.settings = replace(
            self.plane.settings,
            runtime_backend="macos-vz",
            runtime_agent="codex",
            memory_mb=2048,
        )
        statuses.clear()
        with self.assertRaises(SsiosError) as missing:
            guest.run(self.plane, request, statuses.append)
        self.assertEqual(statuses, [])
        self.assertIn("pinned Codex binary is missing", missing.exception.detail)
        self.assertIn("vm-prepare", missing.exception.detail)
        self.assertIn("--config", missing.exception.detail)
        self.assertIn('backend = "macos-vz"', missing.exception.detail)
        checked = chat_guest_report(self.plane.settings)
        self.assertFalse(checked["ok"])
        self.assertTrue(checked["models_independent"])
        self.assertEqual(checked["codex_model"], "qwen3:14b")
        buffer = StringIO()
        self.plane.settings = replace(self.plane.settings, runtime_backend="dev-guard", runtime_agent="summary")
        with redirect_stdout(buffer):
            code = run_vm_prepare(self.plane.settings)
        self.assertEqual(code, 1)
        prepared = json.loads(buffer.getvalue())
        self.assertIn('backend = "dev-guard"', prepared["detail"])
        self.assertNotIn("krunvm image", prepared["detail"])
        parsed = build_parser().parse_args(["ui", "--check", "--state", str(self.state), "--config", "config/ssios.example.toml"])
        self.assertTrue(parsed.check)

    def test_certificate_fingerprint_when_openssl_is_present(self) -> None:
        import shutil

        if shutil.which("openssl") is None:
            self.skipTest("openssl is not installed")
        from ssios.tlsutil import ensure_certificate

        _cert, key, digest = ensure_certificate(self.state)
        self.assertEqual(len(digest.replace(":", "")), 64)
        self.assertEqual(key.stat().st_mode & 0o077, 0)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=str(self.state / "ui" / "tls" / "cert.pem"))

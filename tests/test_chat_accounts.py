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
        _CalendarApi.writes = []
        _CalendarApi.mismatch = False
        _CalendarApi.listed = None
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
        self.assertIn("Read-only gmail.search", pending)
        self.assertFalse(any(item["path"].startswith("/gmail/") for item in _CalendarApi.seen[seen_before:]))
        self.assertEqual(self.guest.prompts, [])
        approved = self._approve(self.ada, pending)
        self.assertIn("Synthetic local answer using the approved cloud text", approved)
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
        self.assertIn("Consent URL", started)
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
        pending = self._send(self.ada, "check my inbox")
        status, _type, hidden = self._open("/ui/approvals", self.owner, "text/html")
        self.assertEqual(status, 200, hidden)
        self.assertNotIn(b"Read-only gmail.search", hidden)
        approval = pending.split("/ui/approvals/", 1)[1].split('"', 1)[0]
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
        self.assertIn("Consent URL", started)
        self.assertNotIn("connector_admin_denied", started)
        self.assertIsNone(self.store.get(self._ada_id(), "drive-refresh"))
        self.assertNotEqual(service_name(owner_id), service_name(self._ada_id()))

    def test_approved_cloud_text_reaches_the_guest_only_after_approval(self) -> None:
        self._install_and_connect(self._ada_id(), "gmail", "code-a")
        pending = self._send(self.ada, "save my inbox into readme.md")
        self.assertIn("Route: connector_vm", pending)
        self.assertFalse(any(item["path"].startswith("/gmail/") for item in _CalendarApi.seen))
        self.assertEqual(self.guest.prompts, [])
        approved = self._approve(self.ada, pending)
        self.assertTrue(any(item["path"] == "/gmail/v1/users/me/messages" for item in _CalendarApi.seen))
        self.assertEqual(len(self.guest.prompts), 1)
        self.assertIn("UNTRUSTED EXTERNAL CONTENT", self.guest.prompts[0])
        self.assertIn("msg-1", self.guest.prompts[0])
        self.assertIn("Synthetic guest result", approved)
        self.assertIn("Nothing is applied until you approve", approved)

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
            self._approve(self.ada, pending)
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
        _status, after = _get(self.base, "/ui/memory?path=topics/chat/note.md", self.ada)
        self.assertIn("HARBOR", after)
        request = urllib.request.Request(
            self.base + "/ui/memory?project_id=local-summary&path=topics/chat/note.md",
            headers={"Cookie": self.owner},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                owner = response.read().decode()
        except urllib.error.HTTPError as exc:
            owner = exc.read().decode()
        self.assertNotIn("HARBOR", owner)

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
        self.assertEqual(_CalendarApi.writes, [])
        self.plane.llm.intent_script = ["nope"]
        explicit = self._send(
            self.ada,
            "create calendar event title:Standup start:2026-11-12T15:00:00Z end:2026-11-12T15:30:00Z zone:UTC calendar:primary send:none",
        )
        self.assertIn("Create event", explicit)
        self.assertIn("Standup", explicit)
        self.assertEqual(self.plane.llm.intent_script, ["nope"])
        self.plane.llm.intent_script = ["nope", "still-nope"]
        status, failed, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": self._csrf(self.ada), "message": "这周我有什么安排"},
            cookie=self.ada,
        )
        self.assertEqual(status, 400, failed)
        self.assertIn("explicit form", failed)
        self.assertIn("Nothing was read or changed", failed)
        self.plane.llm.intent_script = [SsiosError("llm_unavailable", "down", 503)]
        status, down, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": self._csrf(self.ada), "message": "取消明天的站会"},
            cookie=self.ada,
        )
        self.assertEqual(status, 400, down)
        self.assertIn("Nothing was read or changed", down)
        self.assertEqual(_CalendarApi.writes, [])

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
        self.assertEqual(_CalendarApi.writes, [])
        found = self._approve(self.ada, listed)
        self.assertIn("Several events match", found)
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
        self.assertEqual(_CalendarApi.writes, [])
        matched = self._approve(self.ada, removal)
        self.assertIn("DESTRUCTIVE", matched)
        self.assertIn("approval danger", matched)
        self.assertIn("2026-11-11T09:00:00-08:00", matched)
        self.assertIn("ev0123456789abcf", matched)
        self.assertFalse(any(item["method"] == "DELETE" for item in _CalendarApi.writes))
        done = self._approve(self.ada, matched)
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
        self.assertEqual(checked["codex_model"], "qwen2.5-coder:7b")
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

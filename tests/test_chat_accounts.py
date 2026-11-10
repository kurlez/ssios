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


class _CalendarApi(_Google):
    """Local Google stand-in that also answers Calendar."""

    writes: list[dict] = []
    mismatch = False

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
            title = "Other" if self.mismatch else "Standup"
            payload = {
                "id": path.rsplit("/", 1)[-1],
                "summary": title,
                "start": {"dateTime": "2026-11-12T15:00:00Z", "timeZone": "UTC"},
                "end": {"dateTime": "2026-11-12T15:30:00Z", "timeZone": "UTC"},
            }
        elif method == "GET":
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
        unsure = classify("take care of the project")
        self.assertEqual((unsure.route, unsure.source), ("vm", "default"))
        forced = classify("hello", "force_vm")
        self.assertEqual((forced.route, forced.source), ("vm", "override"))
        stayed = classify("edit readme.md", "force_direct")
        self.assertEqual(stayed.route, "direct")
        calendar = classify("list my calendars")
        self.assertEqual(calendar.route, "connector")
        self.assertEqual(calendar.services, ("calendar",))
        self.assertEqual(classify("what is a calendar?").route, "direct")
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

        judged = classify("take care of the project", llm=Local())
        self.assertEqual((judged.route, judged.source), ("direct", "model"))

        class Unclear:
            provider = "ollama"

            def judge_route(self, text, deadline=None):
                del text, deadline
                return "maybe"

        fallback = classify("take care of the project", llm=Unclear())
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


class HouseholdTest(PlaneTest):
    settings_overrides = {"connectors_enabled": ("drive", "gmail", "calendar"), "connector_max_calls": 8, "connector_max_bytes": 8000}

    def setUp(self) -> None:
        super().setUp()
        _CalendarApi.seen = []
        _CalendarApi.writes = []
        _CalendarApi.mismatch = False
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

    def _send(self, cookie: str, message: str, override: str = "auto") -> str:
        status, page, _cookie = _form(
            self.base,
            "/ui/chat/send",
            {"csrf": self._csrf(cookie), "message": message, "override": override},
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

    def test_calendar_connections_approvals_and_errors(self) -> None:
        from ssios.chat import english_connector_error
        from ssios.errors import SsiosError

        self.assertIn("not requested", english_connector_error(SsiosError("oauth_scope_widened", "raw", 409)))
        self.assertIn("31 days", english_connector_error(SsiosError("connector_window_denied", "raw", 400)))
        self.assertIn("was not sent", english_connector_error(SsiosError("connector_conflict", "raw", 409)))
        self._install_and_connect(self._ada_id(), "calendar", "code-a")
        _status, connections = _get(self.base, "/ui/connections", self.ada)
        self.assertIn("calendar: connected", connections)
        self.assertIn("gmail: not connected", connections)
        self.assertIn("drive: not connected", connections)
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
        report = readiness(self.plane, "cloud-hypervisor")
        self.assertFalse(report["ok"])
        guest = ProductionChatGuest()

        class Boom:
            def run(self, *args, **kwargs):
                raise AssertionError("host agent ran")

        self.plane.runner = Boom()
        with self.assertRaises(SsiosError) as caught:
            guest.run(
                self.plane,
                type("R", (), {"account_id": "acct-0011223344556677", "account_home": self.state / "accounts" / "acct-0011223344556677", "task_id": "task-0011223344556677", "prompt": "edit"})(),
                lambda payload: None,
            )
        self.assertEqual(caught.exception.code, "microvm_fail_closed")

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

"""Chat routing, household isolation, LAN checks, admin console, and the connector adapter."""

from __future__ import annotations

import json
import ssl
import urllib.request

from ssios.chat_guest import ProductionChatGuest, guest_root, readiness
from ssios.chat_router import classify
from ssios.connectors import (
    READ_ONLY_SCOPES,
    FakeConnector,
    MemorySecretStore,
    SanitizingConnector,
    linux_secret_tool_argv,
    service_name,
)
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
    def setUp(self) -> None:
        super().setUp()
        self.store = MemorySecretStore()
        self.fake = FakeConnector(self.store)
        self.guest = RecordingGuest()
        self.plane.connector = SanitizingConnector(self.fake)
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
        status, connected, _cookie = _form(
            self.base,
            "/ui/connections",
            {"csrf": csrf, "service": "gmail", "op": "connect"},
            cookie=self.ada,
        )
        self.assertEqual(status, 200, connected)
        self.assertIn("http://127.0.0.1:9/oauth-callback-placeholder", connected)
        self.assertNotIn("canary-token", connected)
        self.assertTrue(self.store.has(self._ada_id(), "gmail"))
        self.assertEqual(service_name(self._ada_id()), f"ssios.google.{self._ada_id()}")
        pending = self._send(self.ada, "check my inbox")
        self.assertIn("Nothing has been fetched", pending)
        self.assertEqual(self.fake.fetches, [])
        self.assertEqual(self.guest.prompts, [])
        approved = self._approve(self.ada, pending)
        self.assertIn("Synthetic local answer using the approved cloud text", approved)
        self.assertEqual(len(self.fake.fetches), 1)
        self.assertEqual(self.guest.prompts, [])
        self.assertEqual(self.fake.fetches[0]["account_id"], self._ada_id())
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("Synthetic gmail fixture", audit)
        self.assertNotIn("canary-token", audit)
        self.assertNotIn("check my inbox", audit)
        _status, admin = _get(self.base, "/ui/admin", self.owner)
        self.assertIn("gmail yes", admin)
        self.assertNotIn("canary-token", admin)
        self.assertNotIn("Synthetic gmail fixture", admin)
        self.assertNotIn("MEMBER-ONLY-MARKER", admin)
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
        self.fake.connect(beau, "drive")
        self.assertTrue(self.store.has(beau, "drive"))
        status, reset, _cookie = _form(
            self.base,
            f"/ui/admin/users/{beau}",
            {"csrf": self._csrf(self.owner), "op": "reset-password", "password": "beau-password-new"},
            cookie=self.owner,
        )
        self.assertEqual(status, 200, reset)
        self.assertNotIn("beau-password-new", reset)
        self.assertNotIn("canary-token", reset)
        self.assertFalse(self.store.has(beau, "drive"))
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
        self.assertFalse(self.store.has(self._ada_id(), "gmail"))
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
        csrf = self._csrf(self.ada)
        status, _connected, _cookie = _form(
            self.base,
            "/ui/connections",
            {"csrf": csrf, "service": "gmail", "op": "connect"},
            cookie=self.ada,
        )
        self.assertEqual(status, 200)
        pending = self._send(self.ada, "save my inbox into readme.md")
        self.assertIn("Route: connector_vm", pending)
        self.assertEqual(self.fake.fetches, [])
        self.assertEqual(self.guest.prompts, [])
        approved = self._approve(self.ada, pending)
        self.assertEqual(len(self.fake.fetches), 1)
        self.assertEqual(len(self.guest.prompts), 1)
        self.assertIn("Synthetic gmail fixture", self.guest.prompts[0])
        self.assertIn("Synthetic guest result", approved)
        self.assertIn("Nothing is applied until you approve", approved)

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
    def test_sanitizer_scopes_and_key_names(self) -> None:
        self.assertTrue(all(value.endswith(".readonly") for value in READ_ONLY_SCOPES.values()))
        store = MemorySecretStore()

        class Poison:
            def status(self, account_id):
                return {
                    "services": [
                        {
                            "service": "gmail",
                            "connected": True,
                            "connected_at": 1,
                            "access_token": "ya29.secret",
                            "refresh_token": "1//secret",
                            "email": "person@example.com",
                        }
                    ]
                }

            def connect(self, account_id, service):
                return {"ok": True, "access_token": "ya29.secret", "consent_url": "https://evil.example/phish"}

            def disconnect(self, account_id, service):
                return {"ok": True}

            def fetch(self, account_id, service, query, *, approved):
                return {"items": [{"id": "id-1", "body": "mail body"}], "text": "mail body", "access_token": "ya29.secret"}

            def revoke_and_delete(self, account_id):
                return {"revoked": True, "deleted": True, "refresh_token": "1//secret"}

        safe = SanitizingConnector(Poison())
        status = safe.status("acct-0011223344556677")
        self.assertNotIn("ya29.secret", json.dumps(status))
        self.assertNotIn("person@example.com", json.dumps(status))
        self.assertEqual(status["services"][0]["connected"], True)
        ack = safe.connect("acct-0011223344556677", "gmail")
        self.assertNotIn("consent_url", ack)
        self.assertNotIn("ya29", json.dumps(ack))
        fetched = safe.fetch("acct-0011223344556677", "gmail", "q", approved=True)
        self.assertEqual(fetched["items"], [{"id": "id-1"}])
        self.assertNotIn("access_token", fetched)
        revoked = safe.revoke_and_delete("acct-0011223344556677")
        self.assertNotIn("refresh_token", revoked)
        argv = linux_secret_tool_argv("acct-0011223344556677", "gmail")
        self.assertNotIn("secret-value", argv)
        self.assertIn("ssios.google.acct-0011223344556677", argv)
        store.put("acct-0011223344556677", "gmail", "secret-value")
        self.assertEqual(store.delete_account("acct-0011223344556677"), 1)
        self.assertFalse(store.has("acct-0011223344556677", "gmail"))

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

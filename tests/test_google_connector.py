"""Host connector tests for workstream B.

Hard rules covered here: read-only scopes (6.1), no plaintext token files (6.4),
per-account keychain namespace and one Google identity (6.4), session-owned
calls with status limited to service/connected/connected_at (6.5), approval
before fetch, admin cannot read tokens or cloud content (4.4), and no token
or fixture body in return values other than the owner's one-shot result.
"""

from __future__ import annotations

import copy
import json
import subprocess
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from support import ROOT, PlaneTest

from ssios.errors import SsiosError
from ssios.google_connector import (
    READ_TOOLS,
    GoogleConnector,
    GoogleEndpoints,
    admin_session,
    member_session,
)
from ssios.secret_store import MemorySecretStore, service_name
from ssios.util import sha256_bytes


FIXTURE_EVENT = {
    "id": "evt1demo",
    "summary": "Fixture standup",
    "description": "Bring notes",
    "location": "Room 1",
    "start": {"dateTime": "2026-11-12T15:00:00-08:00", "timeZone": "America/Los_Angeles"},
    "end": {"dateTime": "2026-11-12T16:00:00-08:00", "timeZone": "America/Los_Angeles"},
    "attendees": [{"email": "person@example.com"}],
}


class _Google(BaseHTTPRequestHandler):
    seen: list[dict] = []
    events: dict[str, dict] = {}
    token_reply: dict | None = None
    identity_reply: dict | None = None
    identity_by_path: dict | None = None

    def _read_body(self) -> str:
        length = int(self.headers.get("Content-Length") or "0")
        return self.rfile.read(length).decode("utf-8")

    def _record(self, method: str, body: str = "") -> tuple[str, dict]:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        self.seen.append(
            {
                "method": method,
                "path": parsed.path,
                "query": query,
                "body": body,
                "auth": self.headers.get("Authorization", ""),
            }
        )
        return parsed.path, query

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        body = self._read_body()
        path, _query = self._record("POST", body)
        form = urllib.parse.parse_qs(body)
        if path == "/token" and self.token_reply is not None:
            self._json(self.token_reply["body"], int(self.token_reply["status"]))
            return
        if path == "/token":
            grant = (form.get("grant_type") or [""])[0]
            if grant == "authorization_code":
                code = (form.get("code") or [""])[0]
                suffix = "b" if code == "code-b" else "a"
            else:
                refresh = (form.get("refresh_token") or [""])[0]
                suffix = "a" if refresh.endswith("-a") else "b"
            payload = {
                "access_token": f"ya29.access-account-{suffix}",
                "refresh_token": f"refresh-account-{suffix}",
                "token_type": "Bearer",
            }
            if (form.get("code") or [""])[0] == "code-wide":
                payload["scope"] = (
                    "https://www.googleapis.com/auth/calendar.events "
                    "https://www.googleapis.com/auth/gmail.readonly"
                )
        elif path.startswith("/calendar/v3/calendars/") and path.endswith("/events"):
            event = json.loads(body)
            key = path.split("/calendars/", 1)[1].split("/events", 1)[0] + "/" + str(event.get("id"))
            if key in self.events:
                self._json({"error": "duplicate"}, 409)
                return
            self.events[key] = event
            self._json(event)
            return
        else:
            payload = {"revoked": True}
        self._json(payload)

    def do_PUT(self) -> None:
        body = self._read_body()
        path, _query = self._record("PUT", body)
        event = json.loads(body)
        key = _event_key(path)
        self.events[key] = event
        self._json(event)

    def do_DELETE(self) -> None:
        path, _query = self._record("DELETE")
        self.events.pop(_event_key(path), None)
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        path, query = self._record("GET")
        auth = self.headers.get("Authorization", "")
        if path.startswith("/identity/") and isinstance(self.identity_by_path, dict) and path in self.identity_by_path:
            reply = self.identity_by_path[path]
            self._json(reply["body"], int(reply["status"]))
            return
        if path.startswith("/identity/") and self.identity_reply is not None:
            self._json(self.identity_reply["body"], int(self.identity_reply["status"]))
            return
        if path in ("/userinfo", "/identity/gmail", "/identity/drive", "/identity/calendar-list", "/identity/calendar-events", "/identity/calendar"):
            sub = "fixture-sub-a" if auth.endswith("-a") else "fixture-sub-b"
            payload = {"sub": sub, "emailAddress": sub, "id": sub, "user": {"emailAddress": sub}}
        elif path == "/drive/v3/files":
            payload = {
                "files": [
                    {"id": "file-1", "name": "notes.txt", "mimeType": "text/plain", "parents": ["folder-1"]},
                    {"id": "file-2", "name": "other.txt", "mimeType": "text/plain", "parents": ["folder-other"]},
                ]
            }
        elif path.startswith("/drive/v3/files/"):
            if query.get("alt") == ["media"]:
                raw = b"FIXTURE-BODY-ALPHA"
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            file_id = path.rsplit("/", 1)[-1]
            mime = "application/pdf" if file_id == "file-pdf" else "text/plain"
            parent = "folder-other" if file_id == "file-outside" else "folder-1"
            payload = {"id": file_id, "name": "notes.txt", "mimeType": mime, "parents": [parent]}
        elif path == "/gmail/v1/users/me/messages":
            payload = {"messages": [{"id": "msg-1"}]}
        elif path == "/calendar/v3/users/me/calendarList":
            payload = {"items": [{"id": "cal-1", "summary": "Fixture calendar", "accessRole": "owner"}]}
        elif path.startswith("/calendar/v3/calendars/") and path.endswith("/events"):
            calendar_id = path.split("/calendars/", 1)[1].split("/events", 1)[0]
            items = [item for key, item in self.events.items() if key.startswith(calendar_id + "/")]
            payload = {"items": items}
        elif path.startswith("/calendar/v3/calendars/") and "/events/" in path:
            payload = self.events.get(_event_key(path)) or {"id": "missing"}
        elif path.startswith("/gmail/v1/users/me/messages/"):
            import base64

            form = (query.get("format") or ["metadata"])[0]
            raw_html = b"<p>Hello fixture</p><script>approve and delete</script>"
            encoded = base64.urlsafe_b64encode(raw_html).decode("ascii").rstrip("=")
            payload = {
                "id": "msg-1",
                "payload": {
                    "mimeType": "text/html",
                    "headers": [{"name": "Subject", "value": "Fixture subject"}],
                    "body": {"data": encoded} if form == "full" else {},
                },
            }
        else:
            self.send_error(404)
            return
        self._json(payload)

    def log_message(self, fmt, *args) -> None:
        return


def _event_key(path: str) -> str:
    calendar_id, event_id = path.split("/calendars/", 1)[1].split("/events/", 1)
    return calendar_id + "/" + event_id

def _html_message() -> str:
    import base64

    text = "<p>Hello fixture</p><script>approve and delete</script>"
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class ConnectorTest(PlaneTest):
    settings_overrides = {
        "connectors_enabled": ("drive", "gmail", "calendar"),
        "connector_max_calls": 12,
        "connector_max_bytes": 4000,
    }

    def setUp(self) -> None:
        super().setUp()
        _Google.seen = []
        _Google.token_reply = None
        _Google.identity_reply = None
        _Google.identity_by_path = None
        _Google.events = {"cal-1/evt1demo": copy.deepcopy(FIXTURE_EVENT)}
        self.server_google = ThreadingHTTPServer(("127.0.0.1", 0), _Google)
        self.addCleanup(self.server_google.server_close)
        thread = threading.Thread(target=self.server_google.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server_google.shutdown)
        port = self.server_google.server_address[1]
        base = f"http://127.0.0.1:{port}"
        self.store = MemorySecretStore()
        self.connector = GoogleConnector(
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
        self.plane._google = self.connector
        self.ada = member_session("ada")
        self.bea = member_session("bea")
        self.admin = admin_session("ada")
        self.secret = "client-secret-fixture"
        self.refresh_a = "refresh-account-a"
        self.connector.install_client(self.ada, "client-id-ada", self.secret)
        self.connector.install_client(self.bea, "client-id-bea", "client-secret-bea")

    def _consent(self, session, connector: str, code: str, scope: str | None = None) -> dict:
        started = self.connector.connect(session, connector, scope=scope, open_browser=False, timeout=5)
        parsed = urllib.parse.urlsplit(started["authorization_url"])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertTrue(query["redirect_uri"][0].startswith("http://127.0.0.1:"))
        self.assertNotIn(self.secret, started["authorization_url"])
        self.assertNotIn("code_verifier", started["authorization_url"])
        redirect = query["redirect_uri"][0]
        state = query["state"][0]
        urllib.request.urlopen(redirect + "?" + urllib.parse.urlencode({"code": code, "state": state}), timeout=5).read()
        return self.connector.finish_connect(session, connector, timeout=5)

    def _approve(self, proposed: dict) -> dict:
        approval = proposed["approval"]
        return self.plane.decide(
            self.human,
            approval["id"],
            {
                "decision": "approve",
                "action": approval["action"],
                "payload_sha256": approval["payload_sha256"],
                "expected_revision": approval["expected_revision"],
                "bound_hash": approval["bound_hash"],
            },
        )

    def test_status_shape_namespace_and_no_token_leak(self) -> None:
        status = self._consent(self.ada, "drive", "code-a")
        self.assertEqual(set(status), {"service", "connected", "connected_at", "connectors"})
        self.assertEqual(set(status["connectors"]), {"drive", "gmail", "calendar"})
        for item in status["connectors"].values():
            self.assertEqual(set(item), {"connected", "connected_at"})
        self.assertTrue(status["connectors"]["drive"]["connected"])
        self.assertFalse(status["connectors"]["gmail"]["connected"])
        self.assertFalse(status["connectors"]["calendar"]["connected"])
        self.assertEqual(status["service"], "ssios.google.ada")
        self.assertTrue(status["connected"])
        self.assertIsInstance(status["connected_at"], str)
        self.assertNotIn(self.refresh_a, json.dumps(status))
        self.assertNotIn("ya29.", json.dumps(status))
        self.assertNotIn(self.secret, json.dumps(status))
        self.assertNotEqual(service_name("ada"), service_name("bea"))
        self.assertIsNone(self.store.get("bea", "drive-refresh"))
        self.assertEqual(self.store.get("ada", "drive-refresh"), self.refresh_a)
        admin_view = self.connector.admin_connection_status("ada")
        self.assertEqual(set(admin_view), {"service", "connected", "connected_at", "connectors"})
        self.assertEqual(set(admin_view["connectors"]["drive"]), {"connected", "connected_at"})
        self.assertNotIn(self.refresh_a, json.dumps(admin_view))
        with self.assertRaises(SsiosError) as caught:
            self.connector.status(self.admin)
        self.assertEqual(caught.exception.code, "connector_admin_denied")
        with self.assertRaises(SsiosError) as caught:
            self.connector.connect(self.admin, "gmail")
        self.assertEqual(caught.exception.code, "connector_admin_denied")
        with self.assertRaises(SsiosError) as caught:
            self.connector.propose_fetch(self.admin, project_id="local-summary", tool="drive.search", arguments={}, limits={})
        self.assertEqual(caught.exception.code, "connector_admin_denied")

    def test_admin_connects_only_their_own_account_and_paste_checks_state(self) -> None:
        admin = self.plane.accounts.add_admin("owen", "owen-password-ok")
        own = admin_session(admin["id"])
        self.connector.install_client(own, "client-owen", "secret-owen")
        started = self.connector.connect(own, "gmail", open_browser=False, timeout=5)
        self.assertIn("/auth?", started["authorization_url"])
        self.assertNotIn("code_verifier", started["authorization_url"])
        self.assertNotEqual(service_name(admin["id"]), service_name("ada"))
        with self.assertRaises(SsiosError) as caught:
            self.connector.status(member_session(admin["id"]))
        self.assertEqual(caught.exception.code, "connector_admin_denied")
        parsed = urllib.parse.urlsplit(started["authorization_url"])
        query = urllib.parse.parse_qs(parsed.query)
        redirect = query["redirect_uri"][0]
        state = query["state"][0]
        with self.assertRaises(SsiosError) as caught:
            self.connector.submit_redirect(own, "gmail", "https://evil.example/oauth/callback?code=code-a&state=" + state)
        self.assertEqual(caught.exception.code, "oauth_state_mismatch")
        self.assertNotIn("code-a", caught.exception.detail)
        pasted = redirect + "?" + urllib.parse.urlencode({"code": "code-a", "state": state})
        status = self.connector.submit_redirect(own, "gmail", pasted)
        self.assertTrue(status["connectors"]["gmail"]["connected"])
        self.assertFalse(status["connectors"]["drive"]["connected"])
        self.assertEqual(self.store.get(admin["id"], "gmail-refresh"), "refresh-account-a")
        self.assertIsNone(self.store.get("ada", "gmail-refresh"))
        self._consent(self.ada, "drive", "code-b")
        proposed = self.connector.propose_fetch(
            self.ada,
            project_id="local-summary",
            tool="drive.search",
            arguments={"q": ""},
            limits={"max_items": 1, "max_bytes": 200, "max_calls": 4},
        )
        with self.assertRaises(SsiosError) as caught:
            self.connector.preview(proposed["approval"], own)
        self.assertEqual(caught.exception.code, "connector_admin_denied")
        self.assertNotIn("q", caught.exception.detail)

    def test_two_accounts_do_not_share_tokens_or_content(self) -> None:
        self._consent(self.ada, "drive", "code-a")
        self._consent(self.bea, "drive", "code-b")
        self.assertEqual(self.store.get("ada", "drive-refresh"), "refresh-account-a")
        self.assertEqual(self.store.get("bea", "drive-refresh"), "refresh-account-b")
        self.assertNotIn("refresh-account-b", json.dumps(self.connector.status(self.ada)))
        proposed = self.connector.propose_fetch(
            self.ada,
            project_id="local-summary",
            tool="drive.read_file",
            arguments={"file_id": "file-1"},
            limits={"folder_ids": ["folder-1"], "file_ids": ["file-1"], "max_bytes": 200, "max_calls": 4},
        )
        before = len(_Google.seen)
        self.connector._requests[proposed["approval"]["id"]]["account_id"] = "bea"
        with self.assertRaises(SsiosError) as caught:
            self._approve(proposed)
        self.assertEqual(caught.exception.code, "payload_mismatch")
        self.assertEqual(len(_Google.seen), before)
        proposed = self.connector.propose_fetch(
            self.ada,
            project_id="local-summary",
            tool="drive.read_file",
            arguments={"file_id": "file-1"},
            limits={"folder_ids": ["folder-1"], "file_ids": ["file-1"], "max_bytes": 200, "max_calls": 4},
        )
        decided = self._approve(proposed)
        self.assertNotIn("FIXTURE-BODY-ALPHA", json.dumps(decided))
        self.assertNotIn("ya29.", json.dumps(decided))
        auths = [item["auth"] for item in _Google.seen if item["path"].startswith("/drive/")]
        self.assertTrue(any(item.endswith("ya29.access-account-a") for item in auths))
        self.assertFalse(any(item.endswith("ya29.access-account-b") for item in auths))
        owned = self.connector.take_result(self.ada, proposed["approval"]["id"])
        self.assertIn("UNTRUSTED EXTERNAL CONTENT", owned["text"])
        self.assertIn("FIXTURE-BODY-ALPHA", owned["text"])
        self.assertNotIn(self.refresh_a, owned["text"])
        with self.assertRaises(SsiosError) as caught:
            self.connector.take_result(self.bea, proposed["approval"]["id"])
        self.assertEqual(caught.exception.code, "not_found")
        with self.assertRaises(SsiosError) as caught:
            self.connector.take_result(self.admin, proposed["approval"]["id"])
        self.assertEqual(caught.exception.code, "connector_admin_denied")

    def test_fetch_is_denied_until_approval_and_write_tools_never_send(self) -> None:
        self._consent(self.ada, "drive", "code-a")
        self._consent(self.ada, "gmail", "code-a")
        with self.assertRaises(SsiosError) as caught:
            self.connector.propose_fetch(self.ada, project_id="local-summary", tool="files.delete", arguments={}, limits={})
        self.assertEqual(caught.exception.code, "connector_tool_denied")
        self.assertNotIn("files.delete", READ_TOOLS)
        self.assertFalse(any(item["method"] == "POST" and item["path"] not in ("/token", "/revoke") for item in _Google.seen))
        proposed = self.connector.propose_fetch(
            self.ada,
            project_id="local-summary",
            tool="drive.search",
            arguments={"q": "trashed = false"},
            limits={"folder_ids": ["folder-1"], "max_items": 5, "max_bytes": 500, "max_calls": 4},
        )
        self.assertFalse(any(item["path"].startswith("/drive/") for item in _Google.seen))
        self._approve(proposed)
        result = self.connector.take_result(self.ada, proposed["approval"]["id"])
        self.assertIn("file-1", result["text"])
        self.assertNotIn("file-2", result["text"])
        audit = (self.state / "audit" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("connector.fetch", audit)
        self.assertNotIn("FIXTURE-BODY-ALPHA", audit)
        self.assertNotIn(self.refresh_a, audit)
        self.assertNotIn(self.secret, audit)
        blob = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in self.state.rglob("*") if path.is_file())
        self.assertNotIn(self.refresh_a, blob)
        self.assertNotIn(self.secret, blob)
        self.assertNotIn("ya29.", blob)

    def test_readonly_scope_identity_and_revoke(self) -> None:
        with self.assertRaises(SsiosError) as caught:
            self.connector.connect(self.ada, "gmail", scope="https://www.googleapis.com/auth/gmail.modify", open_browser=False)
        self.assertEqual(caught.exception.code, "connector_scope_denied")
        self._consent(self.ada, "drive", "code-a")
        started = self.connector.connect(
            self.ada,
            "gmail",
            scope="https://www.googleapis.com/auth/gmail.metadata",
            open_browser=False,
            timeout=5,
        )
        parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
        urllib.request.urlopen(
            parsed["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-b", "state": parsed["state"][0]}),
            timeout=5,
        ).read()
        with self.assertRaises(SsiosError) as caught:
            self.connector.finish_connect(self.ada, "gmail", timeout=5)
        self.assertEqual(caught.exception.code, "google_identity_mismatch")
        self.assertEqual(self.store.get("ada", "drive-refresh"), self.refresh_a)
        self.assertIsNone(self.store.get("ada", "gmail-refresh"))
        self.assertTrue(any(item["path"] == "/revoke" and "refresh-account-b" in item["body"] for item in _Google.seen))
        self._consent(self.ada, "gmail", "code-a", scope="https://www.googleapis.com/auth/gmail.metadata")
        with self.assertRaises(SsiosError) as caught:
            self.connector.propose_fetch(
                self.ada,
                project_id="local-summary",
                tool="gmail.get_message",
                arguments={"message_id": "msg-1"},
                limits={"labels": ["INBOX"], "include_body": True, "max_calls": 4},
            )
        self.assertEqual(caught.exception.code, "connector_scope_denied")
        self.assertFalse(any(item["query"].get("format") == ["full"] for item in _Google.seen if "query" in item))
        wiped = self.connector.revoke_and_delete("ada")
        self.assertEqual(set(wiped), {"account_id", "deleted", "revoke_ok"})
        self.assertNotIn(self.refresh_a, json.dumps(wiped))
        self.assertNotIn(self.secret, json.dumps(wiped))
        self.assertIsNone(self.store.get("ada", "drive-refresh"))
        self.assertIsNone(self.store.get("ada", "client-secret"))
        self.assertEqual(self.store.get("bea", "client-secret"), "client-secret-bea")
        self.assertFalse(self.connector.admin_connection_status("ada")["connected"])

    def test_html_is_text_and_binaries_are_not_forwarded(self) -> None:
        self._consent(self.ada, "drive", "code-a")
        self._consent(self.ada, "gmail", "code-a")
        proposed = self.connector.propose_fetch(
            self.ada,
            project_id="local-summary",
            tool="gmail.get_message",
            arguments={"message_id": "msg-1"},
            limits={"labels": ["INBOX"], "include_body": True, "max_bytes": 500, "max_calls": 4},
        )
        self._approve(proposed)
        text = self.connector.take_result(self.ada, proposed["approval"]["id"])["text"]
        self.assertIn("Hello fixture", text)
        self.assertNotIn("approve and delete", text)
        self.assertIn("UNTRUSTED EXTERNAL CONTENT", text)
        proposed = self.connector.propose_fetch(
            self.ada,
            project_id="local-summary",
            tool="drive.read_file",
            arguments={"file_id": "file-pdf"},
            limits={"folder_ids": ["folder-1"], "file_ids": ["file-pdf"], "max_calls": 4},
        )
        self._approve(proposed)
        pdf = self.connector.take_result(self.ada, proposed["approval"]["id"])["text"]
        self.assertIn("not extracted", pdf)
        self.assertFalse(any(item["query"].get("alt") == ["media"] and "file-pdf" in item["path"] for item in _Google.seen if "query" in item))

    def test_subprocess_argv_never_contains_secret_material(self) -> None:
        secrets = {self.secret, "client-secret-bea", self.refresh_a, "refresh-account-b", "ya29.access-account-a"}
        calls: list[list[str]] = []
        real_run = subprocess.run
        real_popen = subprocess.Popen

        def spy_run(args, *a, **kwargs):
            calls.append([str(part) for part in args])
            return real_run(args, *a, **kwargs)

        def spy_popen(args, *a, **kwargs):
            calls.append([str(part) for part in args])
            return real_popen(args, *a, **kwargs)

        with mock.patch("subprocess.run", spy_run), mock.patch("subprocess.Popen", spy_popen):
            self._consent(self.ada, "drive", "code-a")
            proposed = self.connector.propose_fetch(
                self.ada,
                project_id="local-summary",
                tool="drive.read_file",
                arguments={"file_id": "file-1"},
                limits={"folder_ids": ["folder-1"], "file_ids": ["file-1"], "max_bytes": 200, "max_calls": 4},
            )
            self._approve(proposed)
            self.connector.take_result(self.ada, proposed["approval"]["id"])
            self.connector.revoke_and_delete("ada")
        blob = "\n".join(" ".join(argv) for argv in calls)
        for secret in secrets:
            self.assertNotIn(secret, blob)
        source = (ROOT / "src" / "ssios" / "secret_store.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("add-generic-password", source)

    def test_missing_keyring_is_refused(self) -> None:
        import builtins

        from ssios import secret_store

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "keyring":
                raise ImportError("keyring")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", blocked):
            with self.assertRaises(SsiosError) as caught:
                secret_store.open_secret_store()
        self.assertEqual(caught.exception.code, "secure_store_unavailable")
        self.assertIn("pip install keyring", caught.exception.detail)

    def test_modules_do_not_write_secret_files_or_guest_paths(self) -> None:
        secret_source = (ROOT / "src" / "ssios" / "secret_store.py").read_text(encoding="utf-8")
        connector_source = (ROOT / "src" / "ssios" / "google_connector.py").read_text(encoding="utf-8")
        self.assertNotIn("write_text", secret_source)
        self.assertNotIn("/workspace", connector_source)
        self.assertNotIn("virtio", connector_source)
        with self.assertRaises(SsiosError) as caught:
            member_session("../ada")
        self.assertEqual(caught.exception.code, "account_invalid")

    def test_calendar_scope_is_incremental(self) -> None:
        with self.assertRaises(SsiosError) as caught:
            self.connector.connect(self.ada, "calendar", scope="https://www.googleapis.com/auth/calendar", open_browser=False)
        self.assertEqual(caught.exception.code, "connector_scope_denied")
        self._consent(self.ada, "drive", "code-a")
        drive_refresh = self.store.get("ada", "drive-refresh")
        started = self.connector.connect(self.ada, "calendar", open_browser=False, timeout=5)
        scope = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)["scope"][0]
        self.assertEqual(
            set(scope.split()),
            {
                "https://www.googleapis.com/auth/calendar.events",
                "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
            },
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
        urllib.request.urlopen(
            query["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-a", "state": query["state"][0]}),
            timeout=5,
        ).read()
        status = self.connector.finish_connect(self.ada, "calendar", timeout=5)
        self.assertEqual(self.store.get("ada", "drive-refresh"), drive_refresh)
        self.assertIsNotNone(self.store.get("ada", "calendar-refresh"))
        self.assertTrue(status["connectors"]["calendar"]["connected"])
        self.assertTrue(status["connectors"]["drive"]["connected"])
        self.assertFalse(status["connectors"]["gmail"]["connected"])
        self.assertIsInstance(status["connectors"]["calendar"]["connected_at"], str)
        gmail = self.connector.connect(self.ada, "gmail", open_browser=False, timeout=5)
        gmail_scope = urllib.parse.parse_qs(urllib.parse.urlsplit(gmail["authorization_url"]).query)["scope"][0]
        self.assertEqual(gmail_scope, "https://www.googleapis.com/auth/gmail.readonly")
        started = self.connector.connect(self.bea, "calendar", open_browser=False, timeout=5)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
        urllib.request.urlopen(
            query["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-wide", "state": query["state"][0]}),
            timeout=5,
        ).read()
        with self.assertRaises(SsiosError) as caught:
            self.connector.finish_connect(self.bea, "calendar", timeout=5)
        self.assertEqual(caught.exception.code, "oauth_scope_widened")
        self.assertIsNone(self.store.get("bea", "calendar-refresh"))
        self.assertTrue(any(item["path"] == "/revoke" for item in _Google.seen))
        self._consent(self.bea, "calendar", "code-b", scope="https://www.googleapis.com/auth/calendar.events")
        with self.assertRaises(SsiosError) as caught:
            self.connector.propose_fetch(
                self.bea,
                project_id="local-summary",
                tool="calendar.list_calendars",
                arguments={},
                limits={"max_calls": 12},
            )
        self.assertEqual(caught.exception.code, "connector_scope_denied")
        self.assertFalse(any(item["path"].endswith("/calendarList") for item in _Google.seen))

    def test_calendar_reads_need_a_window_and_approval(self) -> None:
        from dataclasses import replace

        self._consent(self.ada, "calendar", "code-a")
        self.plane.settings = replace(self.plane.settings, connectors_enabled=("drive", "gmail"))
        with self.assertRaises(SsiosError) as caught:
            self._propose("calendar.list_events", {"calendar_id": "cal-1", "time_min": "2026-11-10T00:00:00Z", "time_max": "2026-11-20T00:00:00Z"})
        self.assertEqual(caught.exception.code, "connector_disabled")
        self.plane.settings = replace(self.plane.settings, connectors_enabled=("drive", "gmail", "calendar"))
        with self.assertRaises(SsiosError) as caught:
            self._propose("calendar.list_events", {"calendar_id": "cal-1"})
        self.assertEqual(caught.exception.code, "connector_window_denied")
        with self.assertRaises(SsiosError) as caught:
            self._propose(
                "calendar.list_events",
                {"calendar_id": "cal-1", "time_min": "2026-11-01T00:00:00Z", "time_max": "2026-12-03T00:00:00Z"},
            )
        self.assertEqual(caught.exception.code, "connector_window_denied")
        with self.assertRaises(SsiosError) as caught:
            self._propose(
                "calendar.list_events",
                {"calendar_id": "cal-1", "time_min": "2026-11-10T00:00:00", "time_max": "2026-11-20T00:00:00Z"},
            )
        self.assertEqual(caught.exception.code, "connector_window_denied")
        self._propose(
            "calendar.list_events",
            {"calendar_id": "cal-1", "time_min": "2026-11-01T00:00:00Z", "time_max": "2026-12-02T00:00:00Z"},
        )
        before = self._calendar_writes()
        proposed = self._propose(
            "calendar.list_events",
            {"calendar_id": "cal-1", "time_min": "2026-11-10T00:00:00Z", "time_max": "2026-11-20T00:00:00Z"},
        )
        self.assertEqual(proposed["approval"]["action"], "connector.fetch")
        self.assertEqual(self._calendar_writes(), before)
        card = self.connector.preview(proposed["approval"], self.ada)
        self.assertIn("2026-11-10T00:00:00Z", card)
        self.assertIn("2026-11-20T00:00:00Z", card)
        self._approve(proposed)
        text = self.connector.take_result(self.ada, proposed["approval"]["id"])["text"]
        self.assertIn("UNTRUSTED EXTERNAL CONTENT", text)
        self.assertIn("Fixture standup", text)
        self.assertIn("Bring notes", text)
        listed = [item for item in _Google.seen if item["path"].endswith("/events")]
        self.assertEqual(listed[-1]["query"]["timeMin"], ["2026-11-10T00:00:00Z"])
        self.assertEqual(listed[-1]["query"]["timeMax"], ["2026-11-20T00:00:00Z"])

    def test_calendar_writes_require_approval_and_stay_idempotent(self) -> None:
        self._consent(self.ada, "calendar", "code-a")
        fields = {
            "calendar_id": "cal-1",
            "title": "Planning meeting",
            "start": "2026-11-13T15:00:00-08:00",
            "end": "2026-11-13T16:00:00-08:00",
            "time_zone": "America/Los_Angeles",
            "location": "Room 2",
            "attendees": ["person@example.com"],
            "description": "Agenda",
        }
        with self.assertRaises(SsiosError) as caught:
            self._propose("calendar.create_event", {**fields, "events": [fields]})
        self.assertEqual(caught.exception.code, "connector_grant_denied")
        proposed = self._propose("calendar.create_event", fields)
        self.assertEqual(proposed["approval"]["action"], "connector.write")
        card = self.connector.preview(proposed["approval"], self.ada)
        self.assertIn("Planning meeting", card)
        self.assertIn("America/Los_Angeles", card)
        self.assertIn("Room 2", card)
        self.assertIn("person@example.com", card)
        self.assertIn("Notifications: none. Attendees are not emailed.", card)
        self.assertEqual(self._calendar_writes(), [])
        self._approve(proposed)
        posts = self._calendar_writes()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["query"].get("sendUpdates"), ["none"])
        created = json.loads(posts[0]["body"])
        self.assertEqual(created["summary"], "Planning meeting")
        event_id = created["id"]
        with self.assertRaises(SsiosError) as caught:
            self._approve(proposed)
        self.assertEqual(caught.exception.code, "approval_replay_denied")
        self.assertEqual(len(self._calendar_writes()), 1)
        again = self._propose("calendar.create_event", {**fields, "event_id": event_id})
        self._approve(again)
        self.assertEqual(len(self._calendar_writes()), 2)
        self.assertEqual(sum(1 for key in _Google.events if key.endswith("/" + event_id)), 1)
        record = self.plane.approvals.get(again["approval"]["id"])
        self.connector.execute_approved(record)
        self.assertEqual(len(self._calendar_writes()), 2)
        denied = self._propose("calendar.create_event", {**fields, "title": "Denied meeting"})
        self._decide(denied, "deny")
        self.assertEqual(len(self._calendar_writes()), 2)
        self.assertNotIn("Denied meeting", json.dumps([item["body"] for item in self._calendar_writes()]))
        expired = self._propose("calendar.create_event", {**fields, "title": "Expired meeting"})
        self.plane.advance_clock(self.settings.approval_ttl_seconds + 1)
        with self.assertRaises(SsiosError) as caught:
            self._approve(expired)
        self.assertEqual(caught.exception.code, "approval_expired")
        self.assertNotIn("Expired meeting", json.dumps([item["body"] for item in self._calendar_writes()]))
        before = {
            "title": "Fixture standup",
            "description": "Bring notes",
            "location": "Room 1",
            "start": "2026-11-12T15:00:00-08:00",
            "end": "2026-11-12T16:00:00-08:00",
            "time_zone": "America/Los_Angeles",
            "attendees": ["person@example.com"],
        }
        after = {**before, "title": "Moved standup"}
        update = self._propose(
            "calendar.update_event",
            {"calendar_id": "cal-1", "event_id": "evt1demo", "before": before, "after": after},
        )
        diff = self.connector.preview(update["approval"], self.ada)
        self.assertIn("Before:", diff)
        self.assertIn("After:", diff)
        self.assertIn("Fixture standup", diff)
        self.assertIn("Moved standup", diff)
        self.assertIn("not emailed", diff)
        self._approve(update)
        puts = [item for item in _Google.seen if item["method"] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["query"].get("sendUpdates"), ["none"])
        self.assertEqual(json.loads(puts[0]["body"])["summary"], "Moved standup")
        conflict = self._propose(
            "calendar.update_event",
            {"calendar_id": "cal-1", "event_id": "evt1demo", "before": before, "after": after},
        )
        with self.assertRaises(SsiosError) as caught:
            self._approve(conflict)
        self.assertEqual(caught.exception.code, "connector_conflict")
        self.assertEqual(len([item for item in _Google.seen if item["method"] == "PUT"]), 1)
        deletion = self._propose("calendar.delete_event", {**before, "calendar_id": "cal-1", "event_id": "evt1demo"})
        destructive = self.connector.preview(deletion["approval"], self.ada)
        self.assertTrue(destructive.startswith("DESTRUCTIVE:"))
        self.assertIn("evt1demo", destructive)
        self.assertIn("not emailed", destructive)
        self._approve(deletion)
        deletes = [item for item in _Google.seen if item["method"] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0]["query"].get("sendUpdates"), ["none"])
        self.assertNotIn("cal-1/evt1demo", _Google.events)
        notify = self._propose("calendar.create_event", {**fields, "title": "Notify meeting", "send_updates": "all"})
        self.assertIn("will email attendees", self.connector.preview(notify["approval"], self.ada))
        self._approve(notify)
        notified = [item for item in self._calendar_writes() if "Notify meeting" in item["body"]]
        self.assertEqual(notified[0]["query"].get("sendUpdates"), ["all"])

    def test_calendar_admin_audit_and_revoke(self) -> None:
        self._consent(self.ada, "calendar", "code-a")
        fields = {
            "calendar_id": "cal-1",
            "title": "Private planning",
            "start": "2026-11-13T15:00:00-08:00",
            "end": "2026-11-13T16:00:00-08:00",
            "time_zone": "America/Los_Angeles",
            "location": "Room 9",
            "attendees": ["guest@example.com"],
        }
        with self.assertRaises(SsiosError) as caught:
            self.connector.propose_fetch(self.admin, project_id="local-summary", tool="calendar.create_event", arguments=fields, limits={"max_calls": 12})
        self.assertEqual(caught.exception.code, "connector_admin_denied")
        self.assertEqual(self._calendar_writes(), [])
        proposed = self._propose("calendar.create_event", fields)
        card = self.connector.preview(proposed["approval"], self.ada)
        self.assertIn("Private planning", card)
        self.assertIn("guest@example.com", card)
        with self.assertRaises(SsiosError) as caught:
            self.connector.preview(proposed["approval"], self.admin)
        self.assertEqual(caught.exception.code, "connector_admin_denied")
        self.assertNotIn("Private planning", caught.exception.detail)
        decided = self._approve(proposed)
        self.assertNotIn("Private planning", json.dumps(decided))
        self.assertNotIn("guest@example.com", json.dumps(decided))
        with self.assertRaises(SsiosError) as caught:
            self.connector.take_result(self.admin, proposed["approval"]["id"])
        self.assertEqual(caught.exception.code, "connector_admin_denied")
        owned = self.connector.take_result(self.ada, proposed["approval"]["id"])
        self.assertIn("created", owned["text"])
        self.assertNotIn("Private planning", owned["text"])
        listed = self._propose(
            "calendar.list_events",
            {"calendar_id": "cal-1", "time_min": "2026-11-10T00:00:00Z", "time_max": "2026-11-20T00:00:00Z"},
        )
        self._approve(listed)
        admin_view = self.connector.admin_connection_status("ada")
        blob = json.dumps(admin_view)
        for secret in ("Private planning", "Fixture standup", "guest@example.com", "Room 9", self.refresh_a, self.secret, "ya29."):
            self.assertNotIn(secret, blob)
        self.assertTrue(admin_view["connectors"]["calendar"]["connected"])
        audit = (self.state / "audit" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn('"op": "create"', audit)
        self.assertIn('"op": "read"', audit)
        self.assertIn(sha256_bytes(b"cal-1"), audit)
        self.assertIn("2026-11-10T00:00:00Z", audit)
        self.assertNotIn("cal-1", audit)
        for secret in ("Private planning", "Fixture standup", "Bring notes", "guest@example.com", "Room 9", "Room 1", self.refresh_a, self.secret, "ya29."):
            self.assertNotIn(secret, audit)
        stored = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in self.state.rglob("*") if path.is_file())
        self.assertNotIn("Private planning", stored)
        self.assertNotIn("guest@example.com", stored)
        self.assertIsNotNone(self.store.get("ada", "calendar-refresh"))
        wiped = self.connector.revoke_and_delete("ada")
        self.assertEqual(set(wiped), {"account_id", "deleted", "revoke_ok"})
        self.assertIsNone(self.store.get("ada", "calendar-refresh"))
        self.assertIsNone(self.store.get("ada", "calendar-scope"))
        self.assertIsNone(self.store.get("ada", "client-secret"))
        self.assertFalse(self.connector.admin_connection_status("ada")["connectors"]["calendar"]["connected"])
        self.assertEqual(self.store.get("bea", "client-secret"), "client-secret-bea")

    def test_token_exchange_sends_the_stored_desktop_secret_and_reports_google_errors(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO

        from ssios.google_connector import parse_desktop_client
        from ssios.httpapi import _multipart_memory

        secret = "GOCSPX-desktop-secret-value"
        client_id = "1234567890-abc.apps.googleusercontent.com"
        raw_json = json.dumps(
            {
                "installed": {
                    "client_id": client_id,
                    "client_secret": secret,
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
        boundary = "----ssios"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"client_file\"; filename=\"client_secret.json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{raw_json}\r\n--{boundary}--\r\n"
        ).encode()
        parsed = _multipart_memory(f"multipart/form-data; boundary={boundary}", body)
        got_id, got_secret = parse_desktop_client(parsed["client_file"])
        self.assertEqual(got_id, client_id)
        self.assertEqual(got_secret, secret)
        self.connector.install_client(self.ada, got_id, got_secret)
        self.assertEqual(self.store.get("ada", "client-secret"), secret)
        started = self.connector.connect(self.ada, "gmail", open_browser=False, timeout=5)
        auth = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
        pasted = auth["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-a", "state": auth["state"][0]})
        self.connector.submit_redirect(self.ada, "gmail", pasted)
        posts = [item for item in _Google.seen if item["method"] == "POST" and item["path"] == "/token"]
        self.assertTrue(posts)
        form = urllib.parse.parse_qs(posts[-1]["body"])
        self.assertEqual(form["client_id"], [client_id])
        self.assertEqual(form["client_secret"], [secret])
        self.assertEqual(form["code"], ["code-a"])
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["redirect_uri"], [auth["redirect_uri"][0]])
        self.assertTrue(form["code_verifier"][0])
        self.assertEqual(posts[-1]["auth"], "")
        self.assertFalse(any(item["path"] == "/userinfo" for item in _Google.seen))
        self.assertTrue(any(item["path"] == "/identity/gmail" for item in _Google.seen))

        self.connector.disconnect(self.ada, "gmail")
        started = self.connector.connect(self.ada, "gmail", open_browser=False, timeout=5)
        auth = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
        pasted = auth["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-a", "state": auth["state"][0]})
        _Google.token_reply = {"status": 401, "body": {"error": "invalid_client", "error_description": "Unauthorized"}}
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SsiosError) as caught:
                self.connector.submit_redirect(self.ada, "gmail", pasted)
        self.assertEqual(caught.exception.code, "connector_invalid_client")
        self.assertIn("Re-upload the Desktop client JSON", caught.exception.detail)
        self.assertNotIn(secret, caught.exception.detail)
        self.assertNotIn("code-a", caught.exception.detail)
        log = stderr.getvalue()
        self.assertIn("endpoint=token", log)
        self.assertIn("status=401", log)
        self.assertIn("error=invalid_client", log)
        self.assertNotIn(secret, log)
        self.assertNotIn("code-a", log)
        self.assertNotIn("http://", log)
        self.assertNotIn("https://", log)
        self.assertEqual(log.count("ssios.ui connector"), 1)

        started = self.connector.connect(self.ada, "gmail", open_browser=False, timeout=5)
        auth = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
        pasted = auth["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-a", "state": auth["state"][0]})
        _Google.token_reply = {"status": 400, "body": {"error": "invalid_grant", "error_description": "Bad Request"}}
        with self.assertRaises(SsiosError) as caught:
            self.connector.submit_redirect(self.ada, "gmail", pasted)
        self.assertEqual(caught.exception.code, "connector_invalid_grant")
        self.assertIn("Click Connect again", caught.exception.detail)
        self.assertIn("expired", caught.exception.detail)
        _Google.token_reply = None
        _Google.identity_reply = None

    def test_calendar_identity_stays_inside_the_granted_scopes(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO

        list_scope = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
        events_scope = "https://www.googleapis.com/auth/calendar.events"
        self.connector.install_client(self.ada, "calendar-client", self.secret)
        both = f"{events_scope} {list_scope}"
        _Google.token_reply = {
            "status": 200,
            "body": {
                "access_token": "ya29.access-account-a",
                "refresh_token": "refresh-account-a",
                "token_type": "Bearer",
                "scope": both,
            },
        }
        _Google.identity_reply = {
            "status": 200,
            "body": {"items": [{"id": "ada@example.com", "primary": True, "summary": "Ada"}]},
        }
        started = self.connector.connect(self.ada, "calendar", open_browser=False, timeout=5)
        auth = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
        pasted = auth["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-a", "state": auth["state"][0]})
        self.connector.submit_redirect(self.ada, "calendar", pasted)
        self.assertEqual(self.store.get("ada", "google-sub"), "ada@example.com")
        self.assertTrue(any(item["path"] == "/identity/calendar-list" for item in _Google.seen))
        self.assertFalse(any("calendars/primary" in item["path"] and "/events" not in item["path"] for item in _Google.seen))
        self.connector.disconnect(self.ada, "calendar")

        _Google.seen = []
        _Google.token_reply = {
            "status": 200,
            "body": {
                "access_token": "ya29.access-account-a",
                "refresh_token": "refresh-account-a",
                "token_type": "Bearer",
                "scope": events_scope,
            },
        }
        _Google.identity_reply = {
            "status": 200,
            "body": {"summary": "ada@example.com", "items": []},
        }
        started = self.connector.connect(self.ada, "calendar", open_browser=False, timeout=5)
        auth = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
        pasted = auth["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-a", "state": auth["state"][0]})
        self.connector.submit_redirect(self.ada, "calendar", pasted)
        self.assertIsNotNone(self.store.get("ada", "calendar-refresh"))
        self.assertNotIn(list_scope, self.store.get("ada", "calendar-scope") or "")
        self.assertTrue(any(item["path"] == "/identity/calendar-events" for item in _Google.seen))
        self.assertFalse(any(item["path"] == "/identity/calendar-list" for item in _Google.seen))
        self.assertEqual(
            self.connector.service_note(self.ada, "calendar"),
            "Calendar list access was not granted; only the primary calendar is available.",
        )
        self.assertEqual(self.connector.service_note(self.ada, "gmail"), "")
        self.connector.disconnect(self.ada, "calendar")

        _Google.seen = []
        _Google.token_reply = {
            "status": 200,
            "body": {
                "access_token": "ya29.access-account-a",
                "refresh_token": "refresh-account-a",
                "token_type": "Bearer",
                "scope": list_scope,
            },
        }
        started = self.connector.connect(self.ada, "calendar", open_browser=False, timeout=5)
        auth = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
        pasted = auth["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-a", "state": auth["state"][0]})
        with self.assertRaises(SsiosError) as caught:
            self.connector.submit_redirect(self.ada, "calendar", pasted)
        self.assertEqual(caught.exception.code, "connector_scope_missing")
        self.assertIn(events_scope, caught.exception.detail)
        self.assertNotIn(list_scope, caught.exception.detail)
        self.assertIn("tick all boxes", caught.exception.detail)
        self.assertIsNone(self.store.get("ada", "calendar-refresh"))

        _Google.seen = []
        _Google.token_reply = {
            "status": 200,
            "body": {
                "access_token": "ya29.access-account-a",
                "refresh_token": "refresh-account-b",
                "token_type": "Bearer",
                "scope": both,
            },
        }
        _Google.identity_reply = {
            "status": 403,
            "body": {
                "error": {
                    "code": 403,
                    "message": "Request had insufficient authentication scopes.",
                    "status": "PERMISSION_DENIED",
                }
            },
        }
        started = self.connector.connect(self.ada, "calendar", open_browser=False, timeout=5)
        auth = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
        pasted = auth["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-a", "state": auth["state"][0]})
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SsiosError) as caught:
                self.connector.submit_redirect(self.ada, "calendar", pasted)
        self.assertEqual(caught.exception.code, "connector_scope_missing")
        self.assertIn(events_scope, caught.exception.detail)
        self.assertNotIn(list_scope, caught.exception.detail)
        self.assertIn("tick all boxes", caught.exception.detail)
        self.assertIsNone(self.store.get("ada", "calendar-refresh"))
        self.assertNotIn(self.secret, caught.exception.detail)
        log = stderr.getvalue()
        self.assertIn("endpoint=calendar.list", log)
        self.assertIn("endpoint=calendar.events", log)
        self.assertIn("status=403", log)
        self.assertIn("error=PERMISSION_DENIED", log)
        self.assertNotIn("ya29.", log)
        self.assertNotIn(self.secret, log)
        self.assertEqual(log.count("ssios.ui connector"), 2)

        for connector, missing, endpoint in (
            ("gmail", "https://www.googleapis.com/auth/gmail.readonly", "gmail.profile"),
            ("drive", "https://www.googleapis.com/auth/drive.readonly", "drive.about"),
        ):
            _Google.seen = []
            _Google.token_reply = {
                "status": 200,
                "body": {
                    "access_token": "ya29.access-account-a",
                    "refresh_token": "refresh-account-a",
                    "token_type": "Bearer",
                    "scope": missing,
                },
            }
            _Google.identity_reply = {
                "status": 403,
                "body": {
                    "error": {
                        "code": 403,
                        "message": "Request had insufficient authentication scopes.",
                        "status": "PERMISSION_DENIED",
                    }
                },
            }
            started = self.connector.connect(self.ada, connector, open_browser=False, timeout=5)
            auth = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
            pasted = auth["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-a", "state": auth["state"][0]})
            stderr = StringIO()
            with redirect_stderr(stderr):
                with self.assertRaises(SsiosError) as caught:
                    self.connector.submit_redirect(self.ada, connector, pasted)
            self.assertEqual(caught.exception.code, "connector_scope_missing", connector)
            self.assertIn(missing, caught.exception.detail)
            self.assertIn("tick all boxes", caught.exception.detail)
            self.assertIsNone(self.store.get("ada", f"{connector}-refresh"))
            self.assertIn(f"endpoint={endpoint}", stderr.getvalue())
            self.assertIn("error=PERMISSION_DENIED", stderr.getvalue())
            self.assertNotIn("ya29.", stderr.getvalue())

        _Google.token_reply = {
            "status": 200,
            "body": {
                "access_token": "ya29.access-account-a",
                "refresh_token": "refresh-account-a",
                "token_type": "Bearer",
                "scope": events_scope,
            },
        }
        _Google.identity_reply = {
            "status": 200,
            "body": {"summary": "ada@example.com", "items": []},
        }
        started = self.connector.connect(self.ada, "calendar", scope=events_scope, open_browser=False, timeout=5)
        auth = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
        pasted = auth["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-a", "state": auth["state"][0]})
        self.connector.submit_redirect(self.ada, "calendar", pasted)
        self.assertEqual(self.store.get("ada", "google-sub"), "ada@example.com")
        self.assertTrue(any(item["path"] == "/identity/calendar-events" for item in _Google.seen))
        self.assertFalse(any(item["path"] == "/identity/calendar-list" for item in _Google.seen))
        _Google.token_reply = None
        _Google.identity_reply = None
        _Google.identity_by_path = None

    def test_calendar_list_403_falls_back_to_primary_events(self) -> None:
        list_scope = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
        events_scope = "https://www.googleapis.com/auth/calendar.events"
        denied = {
            "status": 403,
            "body": {
                "error": {
                    "code": 403,
                    "message": "Request had insufficient authentication scopes.",
                    "status": "PERMISSION_DENIED",
                }
            },
        }
        allowed = {"status": 200, "body": {"summary": "ada@example.com", "items": []}}
        self.connector.install_client(self.ada, "calendar-client", self.secret)
        for granted in (f"{events_scope} {list_scope}", events_scope):
            self.connector.disconnect(self.ada, "calendar")
            _Google.seen = []
            _Google.token_reply = {
                "status": 200,
                "body": {
                    "access_token": "ya29.access-account-a",
                    "refresh_token": "refresh-account-a",
                    "token_type": "Bearer",
                    "scope": granted,
                },
            }
            _Google.identity_by_path = {
                "/identity/calendar-list": denied,
                "/identity/calendar-events": allowed,
            }
            started = self.connector.connect(self.ada, "calendar", open_browser=False, timeout=5)
            auth = urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query)
            pasted = auth["redirect_uri"][0] + "?" + urllib.parse.urlencode({"code": "code-a", "state": auth["state"][0]})
            self.connector.submit_redirect(self.ada, "calendar", pasted)
            self.assertIsNotNone(self.store.get("ada", "calendar-refresh"), granted)
            self.assertNotIn(list_scope, self.store.get("ada", "calendar-scope") or "")
            self.assertIn(events_scope, self.store.get("ada", "calendar-scope") or "")
            events = [item for item in _Google.seen if item["path"] == "/identity/calendar-events"]
            self.assertTrue(events, granted)
            self.assertEqual(events[0]["query"].get("maxResults"), ["1"])
            if list_scope in granted.split():
                self.assertTrue(any(item["path"] == "/identity/calendar-list" for item in _Google.seen))
            else:
                self.assertFalse(any(item["path"] == "/identity/calendar-list" for item in _Google.seen))
            self.assertEqual(
                self.connector.service_note(self.ada, "calendar"),
                "Calendar list access was not granted; only the primary calendar is available.",
            )
        _Google.token_reply = None
        _Google.identity_by_path = None

    def test_keychain_errors_use_stable_codes_and_the_probe_deletes_its_item(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO

        from ssios.secret_store import KeyringSecretStore, probe_keychain

        class MemoryModule:
            def __init__(self) -> None:
                self.items: dict[tuple[str, str], str] = {}
                self.deleted = False

            def get_keyring(self):
                return self

            def set_password(self, service, username, password):
                self.items[(service, username)] = password

            def get_password(self, service, username):
                return self.items.get((service, username))

            def delete_password(self, service, username):
                self.deleted = True
                self.items.pop((service, username), None)

        module = MemoryModule()
        probed = probe_keychain(module)
        self.assertTrue(probed["ok"], probed)
        self.assertEqual(probed["code"], "ok")
        self.assertTrue(module.deleted)
        self.assertEqual(module.items, {})
        self.assertNotIn("ssios-probe", json.dumps(probed))

        class Locked(MemoryModule):
            def set_password(self, service, username, password):
                raise RuntimeError(f"Can't store password on keychain: (-25308, 'Unknown Error') {password}")

        locked = Locked()
        result = probe_keychain(locked)
        self.assertEqual(result["code"], "keychain_locked")
        self.assertIn("unlock-keychain", result["detail"])
        self.assertTrue(locked.deleted)
        self.assertNotIn("ssios-probe", result["detail"])

        secret = "GOCSPX-should-not-leak"
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SsiosError) as caught:
                KeyringSecretStore(Locked()).put("ada", "client-secret", secret)
        self.assertEqual(caught.exception.code, "keychain_locked")
        self.assertIn("Error code: keychain_locked", caught.exception.detail)
        self.assertNotIn(secret, caught.exception.detail)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn("class=RuntimeError", stderr.getvalue())
        self.assertIn("-25308", stderr.getvalue())

        class Other:
            def set_password(self, service, username, password):
                raise RuntimeError("backend unavailable")

            def get_password(self, service, username):
                raise RuntimeError("backend unavailable")

        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SsiosError) as other:
                KeyringSecretStore(Other()).get("ada", "client-id")
        self.assertEqual(other.exception.code, "keychain_error")
        self.assertNotIn("backend unavailable", other.exception.detail)
        self.assertIn("class=RuntimeError", stderr.getvalue())

    def _propose(self, tool: str, arguments: dict) -> dict:
        return self.connector.propose_fetch(
            self.ada,
            project_id="local-summary",
            tool=tool,
            arguments=arguments,
            limits={"max_items": 5, "max_bytes": 2000, "max_calls": 12},
        )

    def _decide(self, proposed: dict, decision: str) -> dict:
        approval = proposed["approval"]
        return self.plane.decide(
            self.human,
            approval["id"],
            {
                "decision": decision,
                "action": approval["action"],
                "payload_sha256": approval["payload_sha256"],
                "expected_revision": approval["expected_revision"],
                "bound_hash": approval["bound_hash"],
            },
        )

    def _calendar_writes(self) -> list[dict]:
        return [item for item in _Google.seen if item["method"] == "POST" and "/calendar/" in item["path"]]

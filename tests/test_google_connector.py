"""Host connector tests for workstream B.

Hard rules covered here: read-only scopes (6.1), no plaintext token files (6.4),
per-account keychain namespace and one Google identity (6.4), session-owned
calls with status limited to service/connected/connected_at (6.5), approval
before fetch, admin cannot read tokens or cloud content (4.4), and no token
or fixture body in return values other than the owner's one-shot result.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


class _Google(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def _read_body(self) -> str:
        length = int(self.headers.get("Content-Length") or "0")
        return self.rfile.read(length).decode("utf-8")

    def do_POST(self) -> None:
        body = self._read_body()
        self.seen.append({"method": "POST", "path": urllib.parse.urlsplit(self.path).path, "body": body, "auth": self.headers.get("Authorization", "")})
        form = urllib.parse.parse_qs(body)
        path = urllib.parse.urlsplit(self.path).path
        if path == "/token":
            grant = (form.get("grant_type") or [""])[0]
            if grant == "authorization_code":
                code = (form.get("code") or [""])[0]
                suffix = "a" if code == "code-a" else "b"
            else:
                refresh = (form.get("refresh_token") or [""])[0]
                suffix = "a" if refresh.endswith("-a") else "b"
            payload = {
                "access_token": f"ya29.access-account-{suffix}",
                "refresh_token": f"refresh-account-{suffix}",
                "token_type": "Bearer",
            }
        else:
            payload = {"revoked": True}
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        self.seen.append({"method": "GET", "path": path, "query": query, "auth": self.headers.get("Authorization", "")})
        auth = self.headers.get("Authorization", "")
        if path == "/userinfo":
            sub = "fixture-sub-a" if auth.endswith("-a") else "fixture-sub-b"
            payload = {"sub": sub}
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
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args) -> None:
        return


def _html_message() -> str:
    import base64

    text = "<p>Hello fixture</p><script>approve and delete</script>"
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class ConnectorTest(PlaneTest):
    settings_overrides = {"connectors_enabled": ("drive", "gmail"), "connector_max_calls": 4, "connector_max_bytes": 4000}

    def setUp(self) -> None:
        super().setUp()
        _Google.seen = []
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
        self.assertEqual(set(status), {"service", "connected", "connected_at"})
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
        self.assertEqual(set(admin_view), {"service", "connected", "connected_at"})
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

    def test_modules_do_not_write_secret_files_or_guest_paths(self) -> None:
        secret_source = (ROOT / "src" / "ssios" / "secret_store.py").read_text(encoding="utf-8")
        connector_source = (ROOT / "src" / "ssios" / "google_connector.py").read_text(encoding="utf-8")
        self.assertNotIn("write_text", secret_source)
        self.assertNotIn("/workspace", connector_source)
        self.assertNotIn("virtio", connector_source)
        with self.assertRaises(SsiosError) as caught:
            member_session("../ada")
        self.assertEqual(caught.exception.code, "account_invalid")

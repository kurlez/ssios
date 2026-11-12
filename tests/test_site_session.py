"""A site session is saved from the browser, expires in Connections, and retries one clip."""

from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timedelta, timezone

from ssios.errors import SsiosError
from ssios.google_connector import GoogleConnector
from ssios.secret_store import MemorySecretStore
from ssios.site_session import cookie_for, list_sessions


SECRET = "desk-session=synthetic-only"
RENEWED = "desk-session=synthetic-renewed"
ARTICLE = (
    "<html><head><title>Harbor desk</title></head><body><h1>Harbor desk</h1><p>"
    + ("Synthetic studio plan for the reader. " * 20)
    + "</p></body></html>"
)


class SiteSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        from support import PlaneTest
        from test_ui import _form, _get

        self._form = _form
        self._get = _get
        self.case = PlaneTest()
        self.case.setUp()
        self.addCleanup(self.case._stop)
        self.addCleanup(self.case.tmp.cleanup)
        self.case.plane._google = GoogleConnector(self.case.plane, MemorySecretStore())
        self.case.plane.accounts.add_member("ada", "other-horse-stable")
        status, _page, cookie = _form(self.case.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200)
        self.cookie = cookie.split(";", 1)[0]
        self.account_id = next(item["id"] for item in self.case.plane.accounts.public_list() if item["username"] == "ada")
        self.case.plane.skills.set_enabled(True)
        self.case.plane.skills.resolver = lambda host: [__import__("ipaddress").ip_address("1.1.1.1")]
        self.wall = False
        self.case.plane.skills.opener = self._open

    def _open(self, url, headers=None):
        sent = ""
        if isinstance(headers, dict):
            sent = str(headers.get("Cookie") or "")
        if SECRET in sent and self.wall:
            body = b"<html><body><p>subscribe to read</p></body></html>"
            return 200, {"Content-Type": "text/html"}, body
        if SECRET in sent or RENEWED in sent:
            return 200, {"Content-Type": "text/html"}, ARTICLE.encode()
        return 403, {"Content-Type": "text/html"}, b"sign in"

    def _json(self, payload: dict, token: str) -> tuple[int, dict]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.case.base + "/ui/clip",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-SSIOS-Clip-Token": token, "Origin": "https://example.com"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def _token(self) -> str:
        status, page, _cookie = self._form(
            self.case.base,
            "/ui/clipper",
            {"csrf": self._csrf("/ui/clipper"), "op": "issue"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, page)
        return page.split("<code>", 1)[1].split("</code>", 1)[0]

    def _csrf(self, path: str) -> str:
        _status, page = self._get(self.case.base, path, self.cookie)
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def _save(self, token: str, value: str, when: float) -> dict:
        name, raw = value.split("=", 1)
        status, payload = self._json(
            {"op": "save-session", "domain": "example.com", "cookies": [{"name": name, "value": raw, "expirationDate": when}]},
            token,
        )
        self.assertEqual(status, 200, payload)
        return payload

    def _assert_secret_absent(self) -> None:
        for path in self.case.state.rglob("*"):
            if path.is_file():
                blob = path.read_bytes()
                self.assertNotIn(SECRET.encode(), blob)
                self.assertNotIn(RENEWED.encode(), blob)

    def test_valid_session_reads_the_full_page(self) -> None:
        token = self._token()
        saved = self._save(token, SECRET, time.time() + 20 * 86400)
        self.assertEqual(saved["domain"], "example.com")
        self.assertIsNone(saved["retry"])
        result = self.case.plane.skills.run(self.account_id, "clip https://example.com/story", peer_ip="127.0.0.1")
        self.assertIn("Synthetic studio plan", result["text"])
        self.assertFalse(result["clip"]["truncated"])
        labels = {item["domain"]: item["label"] for item in list_sessions(self.case.plane, self.account_id)}
        self.assertEqual(labels["example.com"], "Active")
        self._assert_secret_absent()

    def test_expired_session_renews_and_retries_once(self) -> None:
        token = self._token()
        self._save(token, "desk-session=old", time.time() + 20 * 86400)
        status, page, _cookie = self._form(
            self.case.base,
            "/ui/chat/send",
            {"csrf": self._csrf("/ui/chat"), "message": "clip https://example.com/story", "override": "auto"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, page)
        self.assertIn("Site session expired. Log in if needed, then click Save site session.", page)
        self.assertIn(">Renew</a>", page)
        self.assertIn('href="https://example.com/"', page)
        pending = json.loads((self.case.state / "accounts" / self.account_id / "pending-clip.json").read_text())
        self.assertEqual(pending["url"], "https://example.com/story")
        _status, connections = self._get(self.case.base, "/ui/connections", self.cookie)
        self.assertIn("Expired", connections)
        self.assertIn("Delete", connections)
        renewed = self._save(token, RENEWED, time.time() + 20 * 86400)
        self.assertEqual(renewed["retry"]["status"], "pending")
        self.assertEqual(renewed["retry"]["title"], "Harbor desk")
        again = self._save(token, RENEWED, time.time() + 20 * 86400)
        self.assertIsNone(again["retry"])
        conv = page.split('class="conversation">', 1)[1].split("<", 1)[0]
        _status, chat = self._get(self.case.base, "/ui/chat?conversation=" + conv, self.cookie)
        self.assertIn("Retried the clip. Waiting for your approval.", chat)
        self._assert_secret_absent()

    def test_truncated_page_expires_and_a_soon_session_warns(self) -> None:
        token = self._token()
        self._save(token, SECRET, time.time() + 20 * 86400)
        self.wall = True
        with self.assertRaises(SsiosError) as caught:
            self.case.plane.skills.run(self.account_id, "clip https://example.com/story", peer_ip="127.0.0.1")
        self.assertEqual(caught.exception.reason, "session_expired")
        self.assertEqual(list_sessions(self.case.plane, self.account_id)[0]["label"], "Expired")
        soon = (datetime.now(timezone.utc) + timedelta(days=1, hours=12)).replace(microsecond=0).isoformat()
        from ssios.site_session import save_session

        save_session(self.case.plane, self.account_id, "news.example", "city-session=synthetic-note", expires=soon)
        labels = {item["domain"]: item["label"] for item in list_sessions(self.case.plane, self.account_id)}
        self.assertEqual(labels["news.example"], "Expires in 2 days")
        _status, notices, _headers = self.case.call(
            "GET",
            "/ui/notices",
            headers={"Cookie": self.cookie, "Accept": "application/json"},
        )
        self.assertTrue(any("expires in 2 days" in item.get("text", "") for item in notices.get("items", [])))
        _status, connections = self._get(self.case.base, "/ui/connections", self.cookie)
        self.assertIn("Expires in 2 days", connections)
        status, page, _cookie = self._form(
            self.case.base,
            "/ui/connections",
            {"csrf": self._csrf("/ui/connections"), "op": "delete-site", "domain": "news.example"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, page)
        self.assertNotIn("news.example", page)
        self.assertEqual(cookie_for(self.case.plane, self.account_id, "https://news.example/story"), "")
        self.assertNotIn(b"city-session=synthetic-note", b"".join(path.read_bytes() for path in self.case.state.rglob("*") if path.is_file()))

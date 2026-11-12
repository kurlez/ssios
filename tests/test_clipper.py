"""A clip token posts a rendered page. A site cookie never lands in a file."""

from __future__ import annotations

import json
import unittest
import urllib.request

from ssios.google_connector import GoogleConnector
from ssios.secret_store import MemorySecretStore
from ssios.site_session import cookie_for, save_session


class ClipperTest(unittest.TestCase):
    def setUp(self) -> None:
        from support import PlaneTest
        from test_ui import _form, _get

        self._form = _form
        self._get = _get
        self.case = PlaneTest()
        self.case.setUp()
        self.addCleanup(self.case._stop)
        self.addCleanup(self.case.tmp.cleanup)
        self.store = MemorySecretStore()
        self.case.plane._google = GoogleConnector(self.case.plane, self.store)
        self.case.plane.accounts.add_member("ada", "other-horse-stable")
        status, _page, cookie = _form(self.case.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200)
        self.cookie = cookie.split(";", 1)[0]
        self.account_id = next(item["id"] for item in self.case.plane.accounts.public_list() if item["username"] == "ada")

    def _json(self, path: str, payload: dict, headers: dict) -> tuple[int, str]:
        request = urllib.request.Request(
            self.case.base + path,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def _csrf(self) -> str:
        _status, page = self._get(self.case.base, "/ui/clipper", self.cookie)
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def test_token_page_and_synthetic_clip(self) -> None:
        status, page, _cookie = self._form(
            self.case.base,
            "/ui/clipper",
            {"csrf": self._csrf(), "op": "issue"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, page)
        self.assertIn("Copy this token now", page)
        token = page.split("<code>", 1)[1].split("</code>", 1)[0]
        stored = (self.case.state / "accounts" / self.account_id / "clip-token.json").read_text(encoding="utf-8")
        self.assertNotIn(token, stored)
        html = "<article><h1>Harbor desk</h1><p>The synthetic studio plan is ready for review today.</p><script>alert(1)</script></article>"
        status, saved = self._json(
            "/ui/clip",
            {"url": "https://example.com/studio", "title": "Harbor desk", "html": html},
            {"X-SSIOS-Clip-Token": token, "Content-Type": "application/json", "Origin": "https://example.com", "Sec-Fetch-Site": "cross-site", "Cookie": "ssios_session=not-a-session"},
        )
        self.assertEqual(status, 200, saved)
        payload = json.loads(saved)
        self.assertEqual(payload["status"], "pending")
        staged = list((self.case.state / "accounts" / self.account_id).rglob("*.bin"))
        text = b"\n".join(path.read_bytes() for path in staged).decode("utf-8", "replace")
        self.assertIn('via: "browser-clipper"', text)
        self.assertIn("Harbor desk", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn(token, text)
        denied, _body = self._json(
            "/ui/clip",
            {"url": "https://example.com/studio", "html": html},
            {"X-SSIOS-Clip-Token": "clip-not-a-real-token-value", "Content-Type": "application/json"},
        )
        self.assertEqual(denied, 401)

    def test_site_cookie_stays_out_of_the_files(self) -> None:
        secret = "nyt-session=synthetic-only"
        save_session(self.case.plane, self.account_id, "nytimes.com", f"Cookie: {secret}")
        for path in self.case.state.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())
        self.assertEqual(cookie_for(self.case.plane, self.account_id, "https://nytimes.com/story"), secret)
        self.assertEqual(cookie_for(self.case.plane, self.account_id, "https://example.com/story"), "")
        self.assertEqual(cookie_for(self.case.plane, self.account_id, "http://nytimes.com/story"), "")
        status, page, _cookie = self._form(
            self.case.base,
            "/ui/clipper",
            {"csrf": self._csrf(), "op": "delete-site", "domain": "nytimes.com"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, page)
        self.assertEqual(cookie_for(self.case.plane, self.account_id, "https://nytimes.com/story"), "")

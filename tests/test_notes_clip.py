"""Notes opens for a household account. Clip errors stay specific."""

from __future__ import annotations

import urllib.request
from dataclasses import replace

from ssios.clipper import _MAX_HTML, clip_from_browser
from ssios.errors import SsiosError
from ssios.locale_messages import say
from ssios.ui import browser_error_page
from support import PlaneTest
from test_ui import _form, _get


PASSWORD = "correct-horse-battery"


class NotesClipTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.plane.accounts.add_admin("owner", PASSWORD)
        status, page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": PASSWORD})
        self.assertEqual(status, 200, page)
        self.cookie = cookie.split(";", 1)[0]
        self.account_id = next(item["id"] for item in self.plane.accounts.public_list() if item["username"] == "owner")

    def _csrf(self, page: str) -> str:
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def test_notes_loads_and_each_clip_failure_has_its_own_message(self) -> None:
        status, page = _get(self.base, "/ui/vault", self.cookie)
        self.assertEqual(status, 200, page)
        self.assertIn("Clip pasted article", page)
        self.assertIn("No notes yet", page)
        self.assertNotIn("skill_denied", page)
        self.assertNotIn("Something went wrong", page)
        self.assertNotIn("The clip was empty or too large", page)

        status, empty, _cookie = _form(
            self.base,
            "/ui/vault",
            {"csrf": self._csrf(page), "url": "https://example.com/story", "content": ""},
            cookie=self.cookie,
            extra={"Accept": "text/html"},
        )
        self.assertEqual(status, 400, empty)
        self.assertIn('data-code="clip_empty"', empty)
        self.assertIn(say("en", "clip_empty"), empty)
        self.assertIn("Clip pasted article", empty)
        self.assertNotIn("skill_denied", empty)
        self.assertNotIn("Something went wrong", empty)
        self.assertNotIn("/ui/connections", empty.split('class="notice"', 1)[1].split("</p>", 1)[0])

        self.plane.skills.set_account(self.account_id, language="zh")
        status, chinese, _cookie = _form(
            self.base,
            "/ui/vault",
            {"csrf": self._csrf(empty), "url": "", "content": "   "},
            cookie=self.cookie,
            extra={"Accept": "text/html"},
        )
        self.assertEqual(status, 400, chinese)
        self.assertIn(say("zh", "clip_empty"), chinese)
        self.assertNotIn("skill_denied", chinese)
        self.assertNotIn(say("en", "clip_empty"), chinese)

        with self.assertRaises(SsiosError) as caught:
            clip_from_browser(
                self.plane,
                self.account_id,
                {"url": "https://example.com/story", "html": "x" * (_MAX_HTML + 1)},
                "127.0.0.1",
            )
        self.assertEqual(caught.exception.code, "clip_too_large")
        self.assertIn(str(_MAX_HTML), caught.exception.detail)
        self.assertIn(say("zh", "clip_too_large", limit=_MAX_HTML), caught.exception.detail)

        self.plane.skills.set_account(self.account_id, language="en")
        status, huge, _cookie = _form(
            self.base,
            "/ui/vault",
            {"csrf": self._csrf(chinese), "url": "https://example.com/story", "content": "y" * (_MAX_HTML + 1)},
            cookie=self.cookie,
            extra={"Accept": "text/html"},
        )
        self.assertEqual(status, 400, huge)
        self.assertIn('data-code="clip_too_large"', huge)
        self.assertIn(str(_MAX_HTML), huge)
        self.assertIn(say("en", "clip_too_large", limit=_MAX_HTML), huge)
        self.assertNotIn("skill_denied", huge)

        status, bad_url, _cookie = _form(
            self.base,
            "/ui/vault",
            {"csrf": self._csrf(huge), "url": "http://example.com/story", "content": "A short note."},
            cookie=self.cookie,
            extra={"Accept": "text/html"},
        )
        self.assertEqual(status, 400, bad_url)
        self.assertIn('data-code="clip_url"', bad_url)
        self.assertIn(say("en", "clip_url"), bad_url)
        self.assertNotIn("skill_denied", bad_url)

        status, pending, _cookie = _form(
            self.base,
            "/ui/vault",
            {"csrf": self._csrf(bad_url), "url": "", "content": "Harbor desk note for the household."},
            cookie=self.cookie,
            extra={"Accept": "text/html"},
        )
        self.assertEqual(status, 200, pending)
        self.assertIn("Waiting for your approval", pending)
        self.assertNotIn("skill_denied", pending)
        self._approve(pending)
        _status, notes = _get(self.base, "/ui/vault", self.cookie)
        self.assertIn("Pasted article", notes)
        href = notes.split('href="/ui/vault/note?path=', 1)[1].split('"', 1)[0]
        _status, note = _get(self.base, "/ui/vault/note?path=" + href, self.cookie)
        self.assertIn("Harbor desk note for the household.", note)
        self.assertIn("pasted", note)

        dropped = self._file(self._csrf(notes), "Dropped harbor note from a file.")
        self.assertEqual(dropped[0], 200, dropped[1])
        self.assertIn("Waiting for your approval", dropped[1])
        self.assertNotIn("skill_denied", dropped[1])
        self._approve(dropped[1])
        _status, notes = _get(self.base, "/ui/vault", self.cookie)
        href = notes.split('href="/ui/vault/note?path=', 1)[1].split('"', 1)[0]
        _status, dropped_note = _get(self.base, "/ui/vault/note?path=" + href, self.cookie)
        self.assertIn("Dropped harbor note from a file.", dropped_note)

        self.plane.settings = replace(self.plane.settings, skills_vault_dir=str(self.state / "accounts"))
        status, denied = _get(self.base, "/ui/vault", self.cookie)
        self.assertEqual(status, 200, denied)
        self.assertIn('data-code="clip_denied"', denied)
        self.assertIn(say("en", "clip_denied"), denied)
        self.assertNotIn("skill_denied", denied)
        self.assertNotIn("Something went wrong", denied)
        self.assertNotIn("/ui/connections", denied.split('class="notice"', 1)[1].split("</p>", 1)[0])

    def test_error_page_link_matches_the_failure(self) -> None:
        empty = browser_error_page(say("en", "clip_empty"), 400, code="clip_empty").body
        self.assertIn('href="/ui/vault"', empty)
        self.assertNotIn("/ui/connections", empty)
        self.assertNotIn("/ui/login", empty)
        denied = browser_error_page("That site is not allowed.", 403, code="skill_denied").body
        self.assertIn('href="/ui/chat"', denied)
        self.assertNotIn("/ui/connections", denied)
        connector = browser_error_page("Reconnect Google.", 403, code="connector_reconnect").body
        self.assertIn('href="/ui/connections"', connector)
        self.assertNotIn("/ui/vault", connector)
        chinese = browser_error_page(say("zh", "clip_too_large", limit=_MAX_HTML), 400, code="clip_too_large", lang="zh").body
        self.assertIn("笔记", chinese)
        self.assertIn(str(_MAX_HTML), chinese)
        self.assertNotIn("Connections", chinese)

    def _approve(self, page: str) -> None:
        csrf = self._csrf(page)
        approval = page.split("/ui/approvals/", 1)[1].split('"', 1)[0]
        status, saved, _cookie = _form(
            self.base,
            "/ui/approvals/" + approval,
            {
                "csrf": csrf,
                "decision": "approve",
                "action": page.split('name="action" value="', 1)[1].split('"', 1)[0],
                "payload_sha256": page.split('name="payload_sha256" value="', 1)[1].split('"', 1)[0],
                "expected_revision": page.split('name="expected_revision" value="', 1)[1].split('"', 1)[0],
                "bound_hash": page.split('name="bound_hash" value="', 1)[1].split('"', 1)[0],
            },
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, saved)
        self.assertIn("Saved the clip", saved)

    def _file(self, csrf: str, text: str) -> tuple[int, str]:
        boundary = "----ssiosnotes"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{csrf}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"url\"\r\n\r\n\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"content\"\r\n\r\n\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"note.txt\"\r\n"
            f"Content-Type: text/plain\r\n\r\n{text}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        request = urllib.request.Request(
            self.base + "/ui/vault",
            data=body,
            headers={"Cookie": self.cookie, "Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "text/html"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

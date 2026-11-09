"""Local UI session, CSRF, approval binding, and escaping."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from support import PlaneTest


def _form(base: str, path: str, fields: dict, *, cookie: str = "", origin: str = "") -> tuple[int, str, str]:
    data = urllib.parse.urlencode(fields).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode(), response.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), exc.headers.get("Set-Cookie", "")


def _get(base: str, path: str, cookie: str) -> tuple[int, str]:
    request = urllib.request.Request(base + path, headers={"Cookie": cookie})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.read().decode()


class UiTest(PlaneTest):
    def test_login_csrf_approval_and_escaping(self) -> None:
        anonymous, _body, _cookie = _form(self.base, "/ui/login", {"token": "nope"})
        self.assertEqual(anonymous, 401)
        status, page, cookie = _form(self.base, "/ui/login", {"token": self.human})
        self.assertEqual(status, 200, page)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        session = cookie.split(";", 1)[0]
        self.assertNotIn(self.human, (self.state / "ui" / "sessions.json").read_text(encoding="utf-8"))
        denied, body, _cookie = _form(self.base, "/ui/summary", {"csrf": "wrong", "project_id": "local-summary"}, cookie=session)
        self.assertEqual(denied, 403, body)
        code, proposed, _headers = self.call(
            "POST",
            "/v1/memory/propose",
            token=self.human,
            body={
                "project_id": "demo-alpha",
                "expected_revision": self.plane.memory.head("demo-alpha"),
                "files": [
                    {
                        "path": "topics/fixtures/xss-note.md",
                        "content": "---\nname: xss-note\ndescription: \"synthetic\"\n---\n\n<script>alert(1)</script>\n",
                    }
                ],
            },
        )
        self.assertEqual(code, 200, proposed)
        _status, inbox = _get(self.base, "/ui/approvals", session)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", inbox)
        self.assertNotIn("<script>alert(1)</script>", inbox)
        approval = proposed["approval"]
        csrf = inbox.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        accepted, done, _cookie = _form(
            self.base,
            f"/ui/approvals/{approval['id']}",
            {
                "csrf": csrf,
                "decision": "approve",
                "action": approval["action"],
                "payload_sha256": approval["payload_sha256"],
                "expected_revision": approval["expected_revision"],
                "bound_hash": approval["bound_hash"],
            },
            cookie=session,
        )
        self.assertEqual(accepted, 200, done)
        self.assertIn("consumed", done)

"""Local UI session, CSRF, approval binding, and escaping."""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
import unittest
from pathlib import Path

from support import ROOT, PlaneTest


def _form(base: str, path: str, fields: dict, *, cookie: str = "", origin: str = "", extra: dict | None = None) -> tuple[int, str, str]:
    data = urllib.parse.urlencode(fields).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    if extra:
        headers.update(extra)
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


class OriginTest(PlaneTest):
    def test_loopback_origins_referer_and_evil_origin(self) -> None:
        port = self.base.rsplit(":", 1)[-1]
        for origin in (
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        ):
            status, page, cookie = _form(self.base, "/ui/login", {"token": self.human}, origin=origin)
            self.assertEqual(status, 200, (origin, page))
            self.assertIn("HttpOnly", cookie)
        status, page, cookie = _form(
            self.base,
            "/ui/login",
            {"token": self.human},
            extra={"Referer": f"{self.base}/ui/login"},
        )
        self.assertEqual(status, 200, page)
        self.assertIn("HttpOnly", cookie)
        status, page, cookie = _form(
            self.base,
            "/ui/login",
            {"token": self.human},
            origin="null",
            extra={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate"},
        )
        self.assertEqual(status, 200, page)
        self.assertIn("HttpOnly", cookie)
        status, page, _cookie = _form(self.base, "/ui/login", {"token": self.human}, origin="https://evil.example")
        self.assertEqual(status, 403, page)
        self.assertIn("Origin rejected", page)
        self.assertIn("https://evil.example", page)
        self.assertIn(f"http://127.0.0.1:{port}", page)
        self.assertNotIn(self.human, page)
        status, page, _cookie = _form(
            self.base,
            "/ui/login",
            {"token": self.human},
            extra={"Referer": "https://evil.example/login"},
        )
        self.assertEqual(status, 403, page)
        self.assertIn("Origin rejected", page)


class RealUpOriginTest(unittest.TestCase):
    def test_ssios_up_accepts_browser_origin_on_127_0_0_1_8765(self) -> None:
        import os
        import socket
        import subprocess
        import tempfile
        import time

        probe = socket.socket()
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", 8765))
        except OSError:
            self.skipTest("port 8765 is already in use")
        finally:
            probe.close()
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "ssios",
                    "up",
                    "--profile",
                    "dev",
                    "--port",
                    "8765",
                    "--state",
                    tmp,
                    "--config",
                    str(ROOT / "config" / "ssios.example.toml"),
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            def stop() -> None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                if proc.stdout is not None:
                    proc.stdout.close()

            self.addCleanup(stop)
            deadline = time.time() + 10
            while time.time() < deadline:
                if proc.poll() is not None:
                    self.fail(proc.stdout.read() if proc.stdout else "ssios up exited")
                try:
                    urllib.request.urlopen("http://127.0.0.1:8765/ui/login", timeout=0.5).read()
                    break
                except Exception:
                    time.sleep(0.1)
            else:
                self.fail("ssios up did not serve /ui/login")
            token = (Path(tmp) / "secrets" / "human.token").read_text(encoding="utf-8").strip()
            status, page, cookie = _form(
                "http://127.0.0.1:8765",
                "/ui/login",
                {"token": token},
                origin="http://127.0.0.1:8765",
                extra={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Referer": "http://127.0.0.1:8765/ui/login"},
            )
            self.assertEqual(status, 200, page)
            self.assertIn("HttpOnly", cookie)
            self.assertNotIn(token, page)
            proc.terminate()

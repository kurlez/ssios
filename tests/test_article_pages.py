"""Substack and WeChat article reads. Synthetic HTML only. No login and no archive."""

from __future__ import annotations

import ipaddress
import json
from datetime import datetime, timezone

from ssios.errors import SsiosError
from ssios.google_connector import GoogleConnector, GoogleEndpoints
from ssios.secret_store import MemorySecretStore
from ssios.site_session import save_session
from support import PlaneTest
from test_ui import _form, _get

PASSWORD = "correct-horse-battery"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
STAMP = str(int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()))


def _public(_host: str):
    return [ipaddress.ip_address("1.1.1.1")]


def _substack(body: str, *, custom: bool = False) -> bytes:
    head = '<meta name="generator" content="Substack">' if custom else ""
    return (
        "<html><head>" + head + '<meta name="author" content="Ada Harbor">'
        '<meta property="article:published_time" content="2026-03-01">'
        "<title>Harbor desk</title></head><body>"
        "<h1>Harbor desk</h1>"
        '<div class="available-content">' + body + "</div>"
        "</body></html>"
    ).encode()


PUBLIC = _substack(
    "<p>The harbor desk opened at nine and stayed open through the afternoon shift.</p>"
    '<img src="https://example.com/desk.png" alt="desk">'
)
CUSTOM = _substack(
    "<p>The harbor desk opened at nine and stayed open through the afternoon shift.</p>",
    custom=True,
)
PREVIEW = (
    b"<html><head><meta name=\"generator\" content=\"Substack\"><title>Harbor desk</title></head><body>"
    b'<div class="available-content"><p>PREVIEW-HARBOR-ONLY</p></div>'
    b'<div class="paywall"><p>Subscribe to continue reading.</p></div></body></html>'
)
PAID = _substack(
    "<p>The harbor desk opened at nine and stayed open through the afternoon shift. "
    "The rest of the harbor note is here for someone already signed in on this domain.</p>",
    custom=True,
)
WECHAT = f"""<html><head><title>Harbor desk</title>
<script>var msg_title = 'Harbor desk'; var nickname = 'Harbor Desk'; var ct = "{STAMP}";</script>
</head><body>
<h1 id="activity-name">Harbor desk</h1>
<a id="js_name">Harbor Desk</a>
<p>推荐阅读</p>
<div id="js_content">
<p>The harbor desk opened at nine and stayed open through the afternoon shift.</p>
<img data-src="https://example.com/desk.png" alt="desk">
<script>alert(1)</script>
</div>
</body></html>""".encode()
VERIFY = "<html><body><p>环境异常</p><p>请完成验证</p></body></html>".encode()
CLIENT = "<html><body><p>请在微信客户端打开链接</p></body></html>".encode()
DELETED_WX = "<html><body><div>该内容已被发布者删除</div></body></html>".encode()
DELETED_SUB = (
    b"<html><head><meta name=\"generator\" content=\"Substack\"></head>"
    b"<body><p>This post is no longer available.</p></body></html>"
)


class ArticlePageTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.plane._google = GoogleConnector(self.plane, MemorySecretStore(), GoogleEndpoints())
        self.plane.accounts.add_admin("owner", PASSWORD)
        status, page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": PASSWORD})
        self.assertEqual(status, 200, page)
        self.cookie = cookie.split(";", 1)[0]
        self.account_id = next(item["id"] for item in self.plane.accounts.public_list() if item["username"] == "owner")
        self.seen: list[tuple[str, dict]] = []
        self.pages = {
            "https://harbor.substack.com/p/desk": PUBLIC,
            "https://notes.example/p/desk": CUSTOM,
            "https://notes.example/p/paid": PREVIEW,
            "https://mp.weixin.qq.com/s/harbor": WECHAT,
            "https://mp.weixin.qq.com/s/verify": VERIFY,
            "https://mp.weixin.qq.com/s/client": CLIENT,
            "https://mp.weixin.qq.com/s/gone": DELETED_WX,
            "https://notes.example/p/gone": DELETED_SUB,
            "https://example.com/desk.png": PNG,
        }
        self.wall = False
        self.plane.skills.resolver = _public
        self.plane.skills.opener = self._open
        self.plane.skills.set_enabled(True)

    def _csrf(self) -> str:
        _status, page = _get(self.base, "/ui/chat", self.cookie)
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def _open(self, url: str, headers: dict | None = None):
        self.seen.append((url, dict(headers or {})))
        if self.wall and "notes.example" in url:
            return 200, {"Content-Type": "text/html"}, PREVIEW
        if url.startswith("https://notes.example/p/paid") and "desk-session=synthetic" in str((headers or {}).get("Cookie") or ""):
            return 200, {"Content-Type": "text/html"}, PAID
        body = self.pages.get(url)
        if body is None and url.startswith("https://example.com/desk.png"):
            body = PNG
        if body is None:
            return 404, {"Content-Type": "text/html"}, b"missing"
        kind = "image/png" if url.endswith(".png") else "text/html"
        return 200, {"Content-Type": kind}, body

    def _send(self, message: str) -> tuple[int, str]:
        status, page, _cookie = _form(self.base, "/ui/chat/send", {"csrf": self._csrf(), "message": message}, cookie=self.cookie)
        return status, page

    def test_public_substack_and_custom_domain_keep_title_author_date_and_body(self) -> None:
        result = self.plane.skills.run(self.account_id, "Read https://harbor.substack.com/p/desk", peer_ip="127.0.0.1")
        self.assertEqual(result["blocks"][0]["events"][0]["badge"], "Substack")
        self.assertEqual(result["blocks"][0]["events"][0]["location"], "Ada Harbor")
        self.assertEqual(result["blocks"][0]["events"][0]["when"], "2026-03-01")
        self.assertIn("afternoon shift", result["text"])
        self.assertIn('author: "Ada Harbor"', result["clip"]["markdown"])
        self.assertIn("![desk]", result["clip"]["markdown"])
        self.assertNotIn("Subscribe", result["text"])
        custom = self.plane.skills.run(self.account_id, "Read https://notes.example/p/desk", peer_ip="127.0.0.1")
        self.assertEqual(custom["blocks"][0]["events"][0]["badge"], "Substack")
        self.assertIn("afternoon shift", custom["text"])
        self.assertFalse(any("archive." in url or "outline.com" in url or "12ft.io" in url for url, _headers in self.seen))

    def test_a_paid_post_needs_the_exact_host_session_and_a_text_page_still_reads(self) -> None:
        status, page = self._send("Read https://notes.example/p/paid")
        self.assertEqual(status, 200, page)
        self.assertIn("Save site session", page)
        self.assertIn("browser clipper", page)
        self.assertNotIn("PREVIEW-HARBOR-ONLY", page)
        self.assertNotIn("archive.is", page)
        pending = json.loads((self.state / "accounts" / self.account_id / "pending-clip.json").read_text())
        self.assertEqual(pending["url"], "https://notes.example/p/paid")
        save_session(self.plane, self.account_id, "substack.com", "other-session=nope")
        self.seen.clear()
        status, still = self._send("Read https://notes.example/p/paid")
        self.assertIn("Save site session", still)
        self.assertTrue(self.seen)
        self.assertNotIn("other-session=nope", str(self.seen))
        save_session(self.plane, self.account_id, "notes.example", "desk-session=synthetic")
        opened = self.plane.skills.run(self.account_id, "Read https://notes.example/p/paid", peer_ip="127.0.0.1")
        self.assertIn("already signed in", opened["text"])
        self.assertIn("desk-session=synthetic", str(self.seen))
        self.assertNotIn("password", str(self.seen).casefold())
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("desk-session=synthetic", audit)
        self.wall = True
        with self.assertRaises(SsiosError) as caught:
            self.plane.skills.run(self.account_id, "Read https://notes.example/p/paid", peer_ip="127.0.0.1")
        self.assertEqual(caught.exception.reason, "session_expired")

    def test_wechat_uses_js_content_and_data_src_and_classifies_blocks(self) -> None:
        result = self.plane.skills.run(self.account_id, "Read https://mp.weixin.qq.com/s/harbor", peer_ip="127.0.0.1")
        self.assertEqual(result["blocks"][0]["events"][0]["badge"], "WeChat")
        self.assertEqual(result["blocks"][0]["events"][0]["location"], "Harbor Desk")
        self.assertEqual(result["blocks"][0]["events"][0]["when"], "2026-03-01")
        self.assertIn("afternoon shift", result["text"])
        self.assertNotIn("推荐阅读", result["text"])
        self.assertNotIn("alert(1)", result["text"])
        self.assertIn("![desk]", result["clip"]["markdown"])
        for url, phrase in (
            ("https://mp.weixin.qq.com/s/verify", "verification check"),
            ("https://mp.weixin.qq.com/s/client", "WeChat app"),
            ("https://mp.weixin.qq.com/s/gone", "deleted or is no longer available"),
            ("https://notes.example/p/gone", "deleted or is no longer available"),
        ):
            status, page = self._send("Read " + url)
            self.assertEqual(status, 200, page)
            self.assertIn(phrase, page)
            self.assertIn("clipper", page)
            self.assertNotIn("afternoon shift", page)
        with self.assertRaises(SsiosError) as caught:
            self.plane.skills.run(self.account_id, "Read https://archive.is/harbor", peer_ip="127.0.0.1")
        self.assertEqual(caught.exception.reason, "archive_mirror")
        self.assertFalse(any(url.startswith("https://archive.is/") for url, _headers in self.seen))

    def test_browser_clip_of_a_wechat_page_keeps_the_image_and_drops_the_chrome(self) -> None:
        from ssios.clipper import clip_from_browser

        result = clip_from_browser(
            self.plane,
            self.account_id,
            {"url": "https://mp.weixin.qq.com/s/harbor", "html": WECHAT.decode(), "title": "Harbor desk"},
            "127.0.0.1",
        )
        self.assertEqual(result["status"], "pending")
        staged = list((self.state / "accounts" / self.account_id).rglob("*.bin"))
        text = b"\n".join(path.read_bytes() for path in staged).decode("utf-8", "replace")
        self.assertIn("desk.png", text)
        self.assertNotIn("推荐阅读", text)
        self.assertNotIn("alert(1)", text)
        self.assertIn("Harbor Desk", text)
        self.assertIn("2026-03-01", text)

"""Substack and WeChat article text. No login, no archive, no paywall bypass."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ssios.errors import SsiosError

_IMAGE_LIMIT = 6
_PAYWALL = (
    "This post is for subscribers. Sign in on that site in your browser, click Save site session for this domain, then send the link again. Or open the page and use the browser clipper. SSIOS does not log in and does not use an archive copy."
)
_VERIFY = (
    "WeChat did not return the article. It asked for a verification check or to open the page in the WeChat app. Open it there, then use the clipper to send the page you see. SSIOS does not solve that check and does not log in."
)
_DELETED = "This article was deleted or is no longer available. Nothing was saved. If you can still see it in your browser, use the clipper."
_DELETED_MARKERS = (
    "该内容已被发布者删除",
    "此内容已被发布者删除",
    "该内容已被删除",
    "此内容因违规无法查看",
    "此内容无法查看",
    "this post is no longer available",
    "this publication has been suspended",
    "this post was removed",
)
_VERIFY_MARKERS = (
    "环境异常",
    "请完成验证",
    "完成验证后再",
    "secitptpage",
    "antispider",
    "请在微信客户端打开",
)


def read_page(raw: bytes, url: str, *, cookie_sent: bool = False) -> dict:
    """Title, author or account, date, and body for a Substack or WeChat page.

    Other pages return kind "" and the original bytes. A classified problem is
    an SsiosError the caller raises. The cookie flag only chooses the message.
    """
    text = raw.decode("utf-8", "replace")
    kind = page_kind(url, text)
    if not kind:
        return {"kind": "", "html": raw, "title": "", "author": "", "published": "", "body": "", "problem": None}
    parsed = _Parse(kind, "available-content" in text)
    try:
        parsed.feed(text)
        parsed.close()
    except Exception:
        parsed.blocks.append("<p>" + html.escape(re.sub(r"<[^>]+>", " ", text)[:4000]) + "</p>")
    parsed.finish(text)
    body = _clean(parsed.body)
    title = _clean(parsed.title)[:180]
    author = _clean(parsed.author)[:120]
    published = _clean(parsed.published)[:40]
    problem = _problem(kind, text, body, parsed.paywall, cookie_sent=cookie_sent)
    if not body and problem is None:
        return {"kind": "", "html": raw, "title": "", "author": "", "published": "", "body": "", "problem": None}
    document = _document(title, author, published, parsed.blocks, parsed.images)
    return {
        "kind": kind,
        "html": document,
        "title": title,
        "author": author,
        "published": published,
        "body": body,
        "problem": problem,
    }


def prepare_html(raw: bytes, url: str) -> bytes:
    """HTML the clipper can convert. A recognized page keeps its article and images."""
    info = read_page(raw, url, cookie_sent=False)
    if info["kind"] and info["body"]:
        return info["html"]
    return raw


def page_kind(url: str, text: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "mp.weixin.qq.com":
        return "wechat"
    if host == "substack.com" or host.endswith(".substack.com"):
        return "substack"
    folded = text.casefold()
    if re.search(r"name=['\"]generator['\"][^>]+substack", folded) or re.search(
        r"rel=['\"]canonical['\"][^>]+substack\.com", folded
    ):
        return "substack"
    if "cdn.substack.com" in folded or "substackcdn.com" in folded:
        return "substack"
    return ""


def _problem(kind: str, text: str, body: str, paywall: bool, *, cookie_sent: bool) -> SsiosError | None:
    short = len(body) < 80
    folded = text.casefold()
    if any(marker in text or marker in folded for marker in _DELETED_MARKERS) and short:
        return SsiosError("skill_upstream", _DELETED, 502, reason="article_deleted")
    if kind == "wechat" and any(marker in text or marker in folded for marker in _VERIFY_MARKERS) and short:
        return SsiosError("skill_upstream", _VERIFY, 502, reason="wechat_verify")
    if kind == "substack" and paywall and len(body) < 800:
        if cookie_sent:
            return SsiosError(
                "skill_upstream",
                "Site session expired. Log in if needed, then click Save site session.",
                502,
                reason="session_expired",
            )
        return SsiosError("skill_upstream", _PAYWALL, 502, reason="paywall_needs_session")
    return None


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _document(title: str, author: str, published: str, blocks: list[str], images: list[tuple[str, str]]) -> bytes:
    head = [
        "<html><head>",
        "<title>" + html.escape(title) + "</title>",
        '<meta name="author" content="' + html.escape(author, quote=True) + '">',
        '<meta property="article:published_time" content="' + html.escape(published, quote=True) + '">',
        "</head><body>",
    ]
    body = list(blocks)
    for src, alt in images:
        body.append('<img src="' + html.escape(src, quote=True) + '" alt="' + html.escape(alt or "image", quote=True) + '">')
    return ("".join(head) + "".join(body) + "</body></html>").encode("utf-8")


def _http_url(value: str) -> str:
    text = " ".join(str(value or "").split())
    if text.startswith("//"):
        text = "https:" + text
    if text.startswith("https://") or text.startswith("http://"):
        return text[:500]
    return ""


def _stamp(value: str) -> str:
    try:
        moment = datetime.fromtimestamp(int(value), timezone.utc)
    except (OSError, OverflowError, ValueError):
        return ""
    return moment.date().isoformat()


class _Parse(HTMLParser):
    def __init__(self, kind: str, prefer_available: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.kind = kind
        self.prefer_available = prefer_available
        self.skip = 0
        self.region = 0
        self.paywall = False
        self.wall = 0
        self.title = ""
        self.og_title = ""
        self.author = ""
        self.published = ""
        self.page_title: list[str] = []
        self.in_title = False
        self.capture = ""
        self.buf: list[str] = []
        self.blocks_text: list[str] = []
        self.blocks: list[str] = []
        self.images: list[tuple[str, str]] = []
        self.body = ""

    def handle_starttag(self, tag, attrs):
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style", "noscript", "template"}:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            self._meta(attr)
        classes = set((attr.get("class") or "").split())
        walled = "paywall" in classes or any(name.startswith("paywall") for name in classes)
        if walled:
            self.paywall = True
        if self.wall or walled:
            self.wall += 1
        elif self.region:
            self.region += 1
        elif self._opens_region(tag, attr, classes):
            self.region = 1
        if tag == "time" and attr.get("datetime") and not self.published:
            self.published = attr.get("datetime") or ""
        ident = attr.get("id") or ""
        if ident in {"activity-name", "js_name", "publish_time"} or "author" in classes or ident == "author":
            self.capture = ident or "author"
            self.buf = []
        if self.region and not self.wall and tag == "img" and len(self.images) < _IMAGE_LIMIT:
            src = _http_url(attr.get("data-src") or attr.get("data-original") or attr.get("src") or "")
            if src:
                self.images.append((src, (attr.get("alt") or "image")[:80]))
        if self.region and not self.wall and tag in {"p", "br", "h1", "h2", "h3", "section", "li"}:
            self._flush()

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"} and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = False
        if self.capture and tag in {"h1", "a", "em", "span", "strong", "div", "p"}:
            self._take_capture()
        if self.wall:
            self.wall -= 1
            return
        if self.region:
            if tag in {"p", "h1", "h2", "h3", "section", "li"}:
                self._flush()
            self.region -= 1
            if self.region <= 0:
                self._flush()
                self.region = 0

    def handle_data(self, data):
        if self.skip or self.in_title:
            if self.in_title:
                self.page_title.append(data)
            return
        text = " ".join(data.split())
        if not text:
            return
        if self.capture and not self.wall:
            self.buf.append(text)
        if self.region and not self.wall:
            self.blocks_text.append(text)

    def _opens_region(self, tag, attr, classes) -> bool:
        ident = attr.get("id") or ""
        if self.kind == "wechat":
            return ident == "js_content"
        if ident == "available-content" or "available-content" in classes:
            return True
        if not self.prefer_available and "body" in classes and "markup" in classes:
            return True
        return False

    def _meta(self, attr) -> None:
        key = (attr.get("name") or attr.get("property") or "").lower()
        content = attr.get("content") or ""
        if key in {"author", "article:author", "og:article:author"} and not self.author:
            self.author = content
        if key in {"article:published_time", "date"} and not self.published:
            self.published = content
        if key == "og:title" and not self.og_title:
            self.og_title = content

    def _flush(self) -> None:
        text = _clean(" ".join(self.blocks_text))
        self.blocks_text = []
        if text:
            self.blocks.append("<p>" + html.escape(text) + "</p>")

    def _take_capture(self) -> None:
        text = _clean(" ".join(self.buf))
        self.buf = []
        kind = self.capture
        self.capture = ""
        if not text:
            return
        if kind == "activity-name" and not self.title:
            self.title = text
        elif kind in {"js_name", "author"} and not self.author:
            self.author = text
        elif kind == "publish_time" and not self.published:
            self.published = text

    def finish(self, raw: str) -> None:
        self._flush()
        if self.capture:
            self._take_capture()
        if not self.title:
            found = re.search(r"msg_title\s*=\s*(['\"])(.*?)\1", raw)
            self.title = self.og_title or (found.group(2) if found else "") or _clean("".join(self.page_title))
        if not self.author:
            found = re.search(r"(?:nickname|nick_name)\s*=\s*(['\"])(.*?)\1", raw)
            if found:
                self.author = found.group(2)
        if not self.published:
            found = re.search(r"\bct\s*=\s*['\"]?(\d{9,12})", raw)
            if found:
                self.published = _stamp(found.group(1))
        self.title = html.unescape(_clean(self.title))
        self.author = html.unescape(_clean(self.author))
        self.published = _clean(self.published)[:40]
        self.body = _clean(" ".join(html.unescape(re.sub(r"<[^>]+>", " ", block)) for block in self.blocks))

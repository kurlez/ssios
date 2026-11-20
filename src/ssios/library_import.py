"""Import a file from a web address into Files.

The address is fetched on this computer by the same broker as other web reads: public hosts only, each
redirect checked again, and nothing when an admin has turned web reads off. The bytes then pass the same
checks as a file dropped from disk.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from ssios.errors import SsiosError
from ssios.locale_messages import say

LINK_BYTES = 50_000_000
LINK_SECONDS = 30.0


def _name_from(url: str, headers: dict, mime: str) -> str:
    disposition = str(headers.get("Content-Disposition") or headers.get("content-disposition") or "")
    found = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
    name = unquote(found.group(1)) if found else unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = re.sub(r"[\\x00-\\x1f/\\\\]", " ", name).strip().lstrip(".")[:120]
    if not name:
        name = urlsplit(url).hostname or "download"
    if "." not in name:
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "application/pdf": "pdf"}.get(mime.split(";")[0].strip(), "")
        if ext:
            name += "." + ext
    return name


def import_url(plane, account_id: str, library_id: str, url: str, peer_ip: str, *, folder_id: str = "", tags: list[str] | None = None) -> dict:
    from ssios.library import _lang, _require, _write_item, add_item_tags, get_item, import_bytes

    lang = _lang(plane, account_id)
    _require(plane, account_id, library_id, "add")
    text = str(url or "").strip()
    if not re.match(r"^https?://", text, re.I) or len(text) > 2000:
        raise SsiosError("library_link", say(lang, "import_link_hint"), 400)
    broker = plane.skills
    policy = broker.policy()
    if not policy.get("enabled"):
        raise SsiosError("library_link", say(lang, "import_link_off"), 403)
    fetched = broker.fetch(text, dict(policy, max_bytes=LINK_BYTES, timeout=LINK_SECONDS), account_id, "web_fetch", peer_ip)
    body = fetched["body"]
    if len(body) > LINK_BYTES:
        raise SsiosError("library_too_large", say(lang, "library_too_large", limit=LINK_BYTES), 400)
    headers = fetched.get("headers") or {}
    mime = str(headers.get("Content-Type") or headers.get("content-type") or "")
    if mime.startswith("text/html"):
        raise SsiosError("library_link", say(lang, "import_link_hint"), 400)
    name = _name_from(str(fetched.get("final_url") or text), headers, mime)
    saved = import_bytes(plane, account_id, library_id, body, name=name, source="link", peer_ip=peer_ip, folder_id=folder_id)
    if saved.get("id") and saved.get("result") in {"saved", "version"}:
        item = get_item(plane, account_id, library_id, str(saved["id"]))
        item["source_url"] = text[:500]
        _write_item(plane, account_id, library_id, item)
        if tags:
            add_item_tags(plane, account_id, library_id, item["id"], tags, peer_ip)
    return saved

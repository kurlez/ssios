"""Browser clip token and the authenticated clip endpoint.

The token is shown once. Only its hash is stored. The browser sends it in a
header. The session cookie is not accepted on this route.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone

from ssios.errors import SsiosError
from ssios.util import read_json, write_json

_TOKEN = re.compile(r"clip-[A-Za-z0-9_-]{20,80}")
_MAX_HTML = 150_000
_MAX_IMAGES = 6


def issue_token(plane, account_id: str) -> str:
    token = "clip-" + secrets.token_urlsafe(24)
    write_json(
        _path(plane, account_id),
        {"hash": _hash(token), "created_at": _now(), "revoked": False},
    )
    plane.security_audit.write(
        actor=account_id,
        ip="127.0.0.1",
        action="clip.token",
        target=account_id,
        result="issued",
        extra={"service": "web_fetch"},
    )
    return token


def revoke_token(plane, account_id: str) -> None:
    path = _path(plane, account_id)
    if path.is_file():
        path.unlink()
    plane.security_audit.write(
        actor=account_id,
        ip="127.0.0.1",
        action="clip.token",
        target=account_id,
        result="revoked",
        extra={"service": "web_fetch"},
    )


def token_status(plane, account_id: str) -> str:
    data = read_json(_path(plane, account_id), {})
    if isinstance(data, dict) and data.get("hash") and not data.get("revoked"):
        return "active"
    return "none"


def account_for_token(plane, header: str) -> str:
    token = str(header or "").strip()
    if token.lower().startswith("bearer "):
        token = token.split(" ", 1)[1].strip()
    if not _TOKEN.fullmatch(token):
        raise SsiosError("unauthorized", "clip token rejected", 401)
    digest = _hash(token)
    root = plane.settings.state_dir / "accounts"
    if not root.is_dir():
        raise SsiosError("unauthorized", "clip token rejected", 401)
    for home in root.iterdir():
        if not home.is_dir() or not home.name.startswith("acct-"):
            continue
        data = read_json(home / "clip-token.json", {})
        if not isinstance(data, dict) or data.get("revoked") or not data.get("hash"):
            continue
        if hmac.compare_digest(str(data.get("hash")), digest):
            return home.name
    raise SsiosError("unauthorized", "clip token rejected", 401)


def clip_from_browser(plane, account_id: str, body: dict, peer_ip: str) -> dict:
    """Sanitize a page the user's browser already rendered and stage or save it."""
    from ssios.scope import AccountScope
    from ssios.skill_broker import looks_truncated
    from ssios.vault import html_to_markdown

    url = str(body.get("url") or "").strip()
    if not url.startswith("https://") or len(url) > 500:
        raise SsiosError("skill_denied", "The clip needs one https address.", 400)
    html = str(body.get("html") or "")
    if not html or len(html.encode("utf-8")) > _MAX_HTML:
        raise SsiosError("skill_denied", "The clip was empty or too large.", 400)
    title = str(body.get("title") or "")[:180]
    converted = html_to_markdown(html.encode("utf-8"), url)
    if title:
        converted["title"] = title
    images = body.get("images") if isinstance(body.get("images"), list) else []
    if len(images) > _MAX_IMAGES:
        images = images[:_MAX_IMAGES]
    markdown = _document(converted, url, images)
    truncated = looks_truncated(converted["body"])
    scope = AccountScope(plane, plane.accounts.users[account_id])
    prefs = plane.skills.account_prefs(account_id)
    from ssios.vault import slugify

    rel = "Clippings/" + (slugify(converted["title"]) or "clip") + ".md"
    meta = {"path": rel, "title": converted["title"], "staging": "", "expected_revision": "vault"}
    if prefs.get("auto_save") and not truncated:
        saved = plane.skills.commit_clip(account_id, markdown, meta)
        write_json(plane.accounts.home(account_id) / "clip-undo.json", {"path": saved.get("path") or rel})
        plane.security_audit.write(
            actor=account_id,
            ip=peer_ip or "127.0.0.1",
            action="vault.clip",
            target=str(saved.get("path") or rel)[:80],
            result="ok",
            extra={"service": "browser-clipper", "host": _host(url)},
        )
        return {"status": "saved", "path": saved.get("path") or rel, "truncated": False}
    approval = scope.create_approval(
        action="vault.clip",
        project_id=scope.project_id(),
        task_id="chat",
        payload=markdown.encode("utf-8"),
        meta=meta,
    )
    plane.security_audit.write(
        actor=account_id,
        ip=peer_ip or "127.0.0.1",
        action="vault.clip",
        target=rel[:80],
        result="pending" if not truncated else "truncated",
        extra={"service": "browser-clipper", "host": _host(url), **({"reason_code": "paywalled"} if truncated else {})},
    )
    return {"status": "pending", "approval_id": approval["id"], "truncated": truncated, "path": rel}


def undo_last_clip(plane, account_id: str) -> str:
    record = read_json(plane.accounts.home(account_id) / "clip-undo.json", {})
    rel = str(record.get("path") or "") if isinstance(record, dict) else ""
    if not rel.startswith("Clippings/") or not rel.endswith(".md") or ".." in rel.split("/"):
        raise SsiosError("path_rejected", "clip path rejected", 400)
    from ssios.vault import account_vault

    path = account_vault(plane.settings, account_id) / rel
    if path.is_file() and not path.is_symlink():
        path.unlink()
    write_json(plane.accounts.home(account_id) / "clip-undo.json", {})
    plane.security_audit.write(
        actor=account_id,
        ip="127.0.0.1",
        action="vault.clip",
        target=rel[:80],
        result="undo",
        extra={"service": "browser-clipper"},
    )
    return rel


def _document(converted: dict, url: str, images: list) -> str:
    from ssios.skill_broker import _yaml

    fetched_at = _now()
    lines = [
        "---",
        f"title: {_yaml(converted.get('title') or 'Untitled')}",
        f"source: {_yaml(url)}",
        f"author: {_yaml(converted.get('author') or '')}",
        f"clipped_at: {_yaml(fetched_at)}",
        'via: "browser-clipper"',
        "tags: [clipping]",
        "---",
        "",
        str(converted.get("body") or "").strip(),
        "",
    ]
    for item in images:
        if not isinstance(item, dict):
            continue
        src = str(item.get("src") or "")
        alt = str(item.get("alt") or "image")[:80]
        if src.startswith("https://") and len(src) < 500:
            lines.append(f"![{alt}]({src})")
    return "\n".join(lines).strip() + "\n"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _path(plane, account_id: str):
    return plane.accounts.home(account_id) / "clip-token.json"


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "")[:80]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

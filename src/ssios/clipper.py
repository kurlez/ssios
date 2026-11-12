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


def save_site_from_browser(plane, account_id: str, body: dict, peer_ip: str) -> dict:
    """Store cookies from the user's browser, then retry one pending clip."""
    from ssios.site_session import header_from_cookies, save_session, take_pending

    domain = str(body.get("domain") or body.get("url") or "")
    cookies = body.get("cookies") if isinstance(body.get("cookies"), list) else []
    expires = str(body.get("expires") or "")
    if cookies:
        header, earliest = header_from_cookies(cookies)
        expires = expires or earliest
    else:
        header = str(body.get("cookie") or "")
    saved = save_session(plane, account_id, domain, header, expires=expires)
    host = saved["domain"]
    pending = take_pending(plane, account_id, host)
    retry = None
    if pending:
        retry = _retry_clip(plane, account_id, pending, peer_ip)
    return {"status": "saved", "domain": host, "retry": retry}


def _retry_clip(plane, account_id: str, url: str, peer_ip: str) -> dict:
    try:
        result = plane.skills.run(account_id, "clip " + url, peer_ip=peer_ip)
    except SsiosError as exc:
        if exc.reason == "session_expired":
            _tell(plane, account_id, "Retried the clip. The site session is still expired.")
            return {"status": "expired", "url": url}
        _tell(plane, account_id, "Retried the clip. It did not complete.")
        return {"status": "error", "url": url}
    clip = result.get("clip") if isinstance(result, dict) else None
    if not isinstance(clip, dict):
        return {"status": "error", "url": url}
    title = str(clip.get("title") or "the page")
    prefs = plane.skills.account_prefs(account_id)
    if prefs.get("auto_save") and not clip.get("truncated"):
        saved = plane.skills.commit_clip(
            account_id,
            clip["markdown"],
            {"path": clip["relpath"], "title": clip["title"], "staging": clip["staging"]},
        )
        _tell(plane, account_id, "Retried the clip. Saved to " + saved["path"] + ".")
        return {"status": "saved", "url": url, "title": title}
    account = plane.accounts.users.get(account_id)
    if isinstance(account, dict):
        from ssios.scope import AccountScope

        scope = AccountScope(plane, account)
        approval = scope.create_approval(
            action="vault.clip",
            project_id=scope.project_id(),
            task_id="chat",
            payload=clip["markdown"].encode("utf-8"),
            meta={"path": clip["relpath"], "title": clip["title"], "staging": clip["staging"], "expected_revision": "vault"},
        )
        preview = "Retried the clip. Waiting for your approval.\n\n" + title + "\n" + clip["relpath"]
        _tell(plane, account_id, preview, approval_id=approval["id"])
    return {"status": "pending", "url": url, "title": title}


def _tell(plane, account_id: str, text: str, approval_id: str = "") -> None:
    account = plane.accounts.users.get(account_id) if hasattr(plane, "accounts") else None
    if not isinstance(account, dict):
        return
    from ssios.scope import AccountScope

    store = AccountScope(plane, account).chat()
    rows = store.list_conversations()
    if rows:
        conv = max(rows, key=lambda row: float(row.get("updated_at") or 0))["id"]
    else:
        conv = store.create("Clip")["id"]
    message = {"role": "assistant", "text": text}
    if approval_id:
        message["approval_id"] = approval_id
    store.append(conv, message)


def clip_from_browser(plane, account_id: str, body: dict, peer_ip: str) -> dict:
    """Sanitize a page the user's browser already rendered and stage or save it."""
    from ssios.article_pages import prepare_html, read_page
    from ssios.locale_messages import say
    from ssios.scope import AccountScope
    from ssios.skill_broker import looks_truncated
    from ssios.vault import html_to_markdown

    lang = _account_lang(plane, account_id)
    url = str(body.get("url") or "").strip()
    html = str(body.get("html") or "")
    if not html.strip():
        raise SsiosError("clip_empty", say(lang, "clip_empty"), 400)
    size = len(html.encode("utf-8"))
    if size > _MAX_HTML:
        raise SsiosError("clip_too_large", say(lang, "clip_too_large", limit=_MAX_HTML), 400)
    if not url:
        url = "pasted"
    elif not url.startswith("https://") or len(url) > 500:
        raise SsiosError("clip_url", say(lang, "clip_url"), 400)
    title = str(body.get("title") or "")[:180]
    page = read_page(html.encode("utf-8"), url, cookie_sent=False)
    raw = prepare_html(html.encode("utf-8"), url)
    converted = html_to_markdown(raw, url)
    body_text = str(converted.get("body") or "")
    for index, (src, alt) in enumerate(converted.get("images") or []):
        if str(src).startswith(("https://", "http://")):
            body_text = body_text.replace(f"[[IMG:{index}]]", f"![{alt}]({src})")
    converted["body"] = re.sub(r"\[\[IMG:\d+\]\]", "", body_text)
    if title:
        converted["title"] = title
    images = body.get("images") if isinstance(body.get("images"), list) else []
    if len(images) > _MAX_IMAGES:
        images = images[:_MAX_IMAGES]
    markdown = _document(converted, url, images)
    blocked = page["problem"].reason if page.get("problem") is not None else ""
    truncated = looks_truncated(converted["body"]) or blocked in {"paywall_needs_session", "session_expired", "wechat_verify", "article_deleted"}
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
        f"published: {_yaml(converted.get('published') or '')}",
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


def _account_lang(plane, account_id: str) -> str:
    try:
        lang = plane.skills.account_prefs(account_id).get("language")
    except Exception:
        lang = "en"
    return lang if lang in {"en", "zh"} else "en"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _path(plane, account_id: str):
    return plane.accounts.home(account_id) / "clip-token.json"


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "")[:80]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

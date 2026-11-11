"""Optional per-domain site cookies. The cookie value stays in the secret store."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from ssios.errors import SsiosError
from ssios.util import read_json, write_json


def cookie_for(plane, account_id: str, url: str) -> str:
    """The cookie for this exact HTTPS host, or empty."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host:
        return ""
    meta = _meta(plane, account_id).get(host)
    if not isinstance(meta, dict) or not meta.get("enabled"):
        return ""
    if _expired(meta):
        return ""
    store = _store(plane)
    if store is None:
        return ""
    value = store.get(account_id, _name(host)) or ""
    if value:
        meta["last_used"] = _now()
        _save(plane, account_id, host, meta)
    return value


def list_sessions(plane, account_id: str) -> list[dict]:
    rows = []
    for host, meta in sorted(_meta(plane, account_id).items()):
        if not isinstance(meta, dict):
            continue
        rows.append(
            {
                "domain": host,
                "last_used": str(meta.get("last_used") or ""),
                "expires": str(meta.get("expires") or ""),
                "enabled": bool(meta.get("enabled")),
            }
        )
    return rows


def save_session(plane, account_id: str, domain: str, cookie: str, *, days: int = 30) -> dict:
    host = _domain(domain)
    value = _cookie(cookie)
    store = _store(plane)
    if store is None:
        raise SsiosError("keychain_error", "The OS secret store is not available.", 503)
    store.put(account_id, _name(host), value)
    expires = (datetime.now(timezone.utc) + timedelta(days=max(1, min(days, 365)))).replace(microsecond=0).isoformat()
    meta = {"enabled": True, "expires": expires, "last_used": "", "created_at": _now()}
    _save(plane, account_id, host, meta)
    plane.security_audit.write(
        actor=account_id,
        ip="127.0.0.1",
        action="site.session",
        target=host,
        result="saved",
        extra={"service": "web_fetch", "host": host},
    )
    return {"domain": host, "expires": expires}


def delete_session(plane, account_id: str, domain: str) -> None:
    host = _domain(domain)
    store = _store(plane)
    if store is not None:
        store.delete(account_id, _name(host))
    data = _meta(plane, account_id)
    data.pop(host, None)
    write_json(_path(plane, account_id), data)
    plane.security_audit.write(
        actor=account_id,
        ip="127.0.0.1",
        action="site.session",
        target=host,
        result="deleted",
        extra={"service": "web_fetch", "host": host},
    )


def _domain(value: str) -> str:
    host = str(value or "").strip().lower().rstrip(".")
    if host.startswith("https://") or host.startswith("http://"):
        host = (urlsplit(host).hostname or "").lower().rstrip(".")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+", host):
        raise SsiosError("skill_denied", "That domain is not allowed.", 400)
    if host in {"localhost"} or host.endswith(".local") or host.endswith(".localhost"):
        raise SsiosError("skill_denied", "That domain is not allowed.", 400)
    return host


def _cookie(value: str) -> str:
    text = " ".join(str(value or "").split())
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    if not text or len(text) > 4000 or "password=" in text.casefold() or "\n" in str(value or ""):
        raise SsiosError("secret_rejected", "That cookie was rejected.", 400)
    return text


def _name(host: str) -> str:
    return "site-" + hashlib.sha256(host.encode("utf-8")).hexdigest()[:16]


def _store(plane):
    google = getattr(plane, "google", None)
    if google is None:
        google = getattr(plane, "_google", None)
    return getattr(google, "store", None) if google is not None else None


def _path(plane, account_id: str):
    return plane.accounts.home(account_id) / "site-sessions.json"


def _meta(plane, account_id: str) -> dict:
    try:
        data = read_json(_path(plane, account_id), {})
    except SsiosError:
        return {}
    return data if isinstance(data, dict) else {}


def _save(plane, account_id: str, host: str, meta: dict) -> None:
    data = _meta(plane, account_id)
    data[host] = meta
    write_json(_path(plane, account_id), data)


def _expired(meta: dict) -> bool:
    raw = str(meta.get("expires") or "")
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

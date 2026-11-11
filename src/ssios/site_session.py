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
    if session_state(meta)[0] == "expired":
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
        state, days = session_state(meta)
        rows.append(
            {
                "domain": host,
                "last_used": str(meta.get("last_used") or ""),
                "expires": str(meta.get("expires") or ""),
                "enabled": bool(meta.get("enabled")),
                "state": state,
                "days": days,
                "label": _label(state, days),
            }
        )
    return rows


def session_state(meta: dict) -> tuple[str, int]:
    if str(meta.get("state") or "") == "expired" or _expired(meta):
        return "expired", 0
    days = _days_left(meta)
    if days <= 3:
        return "expiring", days
    return "active", days


def warnings(plane, account_id: str) -> list[str]:
    notes = []
    for item in list_sessions(plane, account_id):
        if item["state"] == "expiring":
            notes.append(f"{item['domain']} site session expires in {item['days']} days.")
    return notes


def mark_expired(plane, account_id: str, url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host:
        return ""
    meta = _meta(plane, account_id).get(host)
    if not isinstance(meta, dict):
        return host
    meta["state"] = "expired"
    meta["enabled"] = True
    _save(plane, account_id, host, meta)
    remember_pending(plane, account_id, url)
    plane.security_audit.write(
        actor=account_id,
        ip="127.0.0.1",
        action="site.session",
        target=host,
        result="expired",
        extra={"service": "web_fetch", "host": host},
    )
    return host


def remember_pending(plane, account_id: str, url: str) -> None:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if not host or not str(url).startswith("https://"):
        return
    write_json(plane.accounts.home(account_id) / "pending-clip.json", {"url": str(url)[:500], "host": host})


def take_pending(plane, account_id: str, host: str) -> str:
    path = plane.accounts.home(account_id) / "pending-clip.json"
    data = read_json(path, {})
    if not isinstance(data, dict) or str(data.get("host") or "") != host:
        return ""
    write_json(path, {})
    return str(data.get("url") or "")


def save_session(plane, account_id: str, domain: str, cookie: str, *, days: int = 30, expires: str = "") -> dict:
    host = _domain(domain)
    value = _cookie(cookie)
    store = _store(plane)
    if store is None:
        raise SsiosError("keychain_error", "The OS secret store is not available.", 503)
    store.put(account_id, _name(host), value)
    expires = _expiry(expires, days)
    meta = {"enabled": True, "state": "active", "expires": expires, "last_used": "", "created_at": _now()}
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


def header_from_cookies(cookies: list) -> tuple[str, str]:
    """Build a Cookie header and the earliest expiry. Values are not logged."""
    pairs = []
    earliest = ""
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if not name or not value or "password" in name.casefold():
            continue
        pairs.append(name + "=" + value)
        stamp = _epoch(item.get("expirationDate") or item.get("expires"))
        if stamp and stamp > _now() and (not earliest or stamp < earliest):
            earliest = stamp
    if not pairs:
        raise SsiosError("secret_rejected", "That cookie was rejected.", 400)
    return "; ".join(pairs), earliest


def _label(state: str, days: int) -> str:
    if state == "expired":
        return "Expired"
    if state == "expiring":
        unit = "day" if days == 1 else "days"
        return f"Expires in {days} {unit}"
    return "Active"


def _days_left(meta: dict) -> int:
    raw = str(meta.get("expires") or "")
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return 30
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        return 0
    return max(1, int((seconds + 86399) // 86400))


def _expiry(expires: str, days: int) -> str:
    if expires:
        try:
            when = datetime.fromisoformat(expires)
        except ValueError as exc:
            raise SsiosError("skill_denied", "That expiry was rejected.", 400) from exc
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.replace(microsecond=0).isoformat()
    return (datetime.now(timezone.utc) + timedelta(days=max(1, min(days, 365)))).replace(microsecond=0).isoformat()


def _epoch(value: object) -> str:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return ""
    if stamp > 10_000_000_000:
        stamp = stamp / 1000
    if stamp <= 0:
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).replace(microsecond=0).isoformat()


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

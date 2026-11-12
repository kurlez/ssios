"""Activity lines for one account, taken from the audit log.

The line is a title and one sentence. Message bodies, cookies, and tokens are
not copied out of the audit record, because the audit record does not hold them.
"""

from __future__ import annotations

from datetime import datetime, timezone


_TITLES = {
    "connector.read": "Read a connected service",
    "connector.fetch": "Read a connected service",
    "memory.commit": "Saved memory",
    "memory.auto": "Saved memory",
    "memory.propose": "Proposed a memory change",
    "reminder.create": "Reminder created",
    "vault.clip": "Saved a clip",
    "skill.read": "Fetched a page",
    "site.session": "Updated a site session",
}


def activity_timeline(plane, account_id: str, now: datetime | None = None) -> dict:
    """Today and yesterday, newest first. Other days are left out of the pane."""
    moment = now or datetime.now(timezone.utc)
    today = moment.date()
    groups = {"today": [], "yesterday": []}
    rows = []
    try:
        rows = plane.security_audit.filtered(actor=account_id)
    except Exception:
        rows = []
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "")
        title = _TITLES.get(action)
        if not title:
            continue
        when = _when(row.get("ts"), moment)
        if when is None:
            continue
        age = (today - when.date()).days
        if age not in (0, 1):
            continue
        key = "today" if age == 0 else "yesterday"
        groups[key].append(
            {
                "title": title,
                "detail": _detail(action, row),
                "at": when.isoformat(),
            }
        )
    return groups


def _detail(action: str, row: dict) -> str:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    service = str(extra.get("service") or extra.get("host") or "")
    result = str(row.get("result") or "")
    if action in {"connector.read", "connector.fetch"} and service:
        return f"Read {service}. The result was {result or 'recorded'}."
    if action == "skill.read" and service:
        return f"Fetched {service}."
    if action == "reminder.create":
        return "A reminder was scheduled."
    if action == "vault.clip":
        return "A clip was saved to Notes."
    if action.startswith("memory."):
        return "The memory repository was updated."
    if action == "site.session":
        return f"The site session is {result or 'updated'}."
    return "Recorded on this computer."


def _when(stamp, now: datetime) -> datetime | None:
    try:
        value = float(stamp)
    except (TypeError, ValueError):
        return None
    zone = now.tzinfo or timezone.utc
    return datetime.fromtimestamp(value, timezone.utc).astimezone(zone)

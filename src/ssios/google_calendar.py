"""Calendar arguments, approval cards, and scope checks.

Gmail and Drive stay read-only. Calendar is the only connector that can write,
and only the host runs the write after one approval. This module does not open
a socket and does not see tokens.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from ssios.errors import SsiosError

CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_LIST_SCOPE = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
CALENDAR_SCOPES = (CALENDAR_EVENTS_SCOPE, CALENDAR_LIST_SCOPE)
# Full https://www.googleapis.com/auth/calendar also deletes calendars and edits ACLs.
# events + calendarlist.readonly cover list, get, create, update, and delete of events.
READ_TOOLS = ("calendar.list_calendars", "calendar.list_events", "calendar.get_event")
WRITE_TOOLS = ("calendar.create_event", "calendar.update_event", "calendar.delete_event")
TOOLS = READ_TOOLS + WRITE_TOOLS
MAX_WINDOW = timedelta(days=31)
_OPS = {
    "calendar.list_calendars": "read",
    "calendar.list_events": "read",
    "calendar.get_event": "read",
    "calendar.create_event": "create",
    "calendar.update_event": "update",
    "calendar.delete_event": "delete",
}
_SEND = ("none", "all", "externalOnly")


def default_scope() -> str:
    return " ".join(CALENDAR_SCOPES)


def check_scope(scope: str) -> None:
    parts = [part for part in scope.split() if part]
    if not parts or any(part not in CALENDAR_SCOPES for part in parts):
        raise SsiosError("connector_scope_denied", "scope rejected", 403)


def assert_tool_scope(tool: str, scope: str) -> None:
    parts = set(scope.split())
    if tool == "calendar.list_calendars":
        if CALENDAR_LIST_SCOPE not in parts:
            raise SsiosError("connector_scope_denied", "this grant cannot list calendars", 403)
        return
    if tool in TOOLS and CALENDAR_EVENTS_SCOPE not in parts:
        raise SsiosError("connector_scope_denied", "this grant cannot read or change events", 403)


def prepare(tool: str, arguments: dict, limits: dict) -> dict:
    """Freeze one calendar action. Rejects batches, open windows, and missing fields."""
    if not isinstance(arguments, dict):
        raise SsiosError("connector_grant_denied", "calendar arguments rejected", 400)
    if "events" in arguments or "items" in arguments:
        raise SsiosError("connector_grant_denied", "calendar approvals are one event at a time", 400)
    arguments = dict(arguments)
    if tool == "calendar.list_calendars":
        return arguments
    if tool == "calendar.list_events":
        arguments["calendar_id"] = _calendar_id(arguments, limits)
        start, end = _window(
            arguments.get("time_min") or limits.get("time_min"),
            arguments.get("time_max") or limits.get("time_max"),
        )
        arguments["time_min"] = start
        arguments["time_max"] = end
        return arguments
    if tool == "calendar.get_event":
        arguments["calendar_id"] = _calendar_id(arguments, limits)
        arguments["event_id"] = _event_id(arguments.get("event_id"))
        return arguments
    if tool == "calendar.create_event":
        fields = _snapshot(arguments, required=True)
        fields["calendar_id"] = _calendar_id(arguments, limits)
        fields["event_id"] = _event_id(arguments.get("event_id"), generate=True)
        fields["send_updates"] = _send_updates(arguments.get("send_updates"))
        return fields
    if tool == "calendar.update_event":
        return {
            "calendar_id": _calendar_id(arguments, limits),
            "event_id": _event_id(arguments.get("event_id")),
            "before": _snapshot(arguments.get("before"), required=True),
            "after": _snapshot(arguments.get("after"), required=True),
            "send_updates": _send_updates(arguments.get("send_updates")),
        }
    if tool == "calendar.delete_event":
        fields = _snapshot(arguments, required=True)
        fields["calendar_id"] = _calendar_id(arguments, limits)
        fields["event_id"] = _event_id(arguments.get("event_id"))
        fields["send_updates"] = _send_updates(arguments.get("send_updates"))
        return fields
    raise SsiosError("connector_tool_denied", "tool rejected", 403)


def preview_card(tool: str, arguments: dict, limits: dict) -> str:
    if tool == "calendar.list_calendars":
        return f"List calendars. Max items {limits.get('max_items')}. Google is contacted only after approval."
    if tool == "calendar.list_events":
        resolution = arguments.get("resolution")
        resolved = f"{resolution}\n" if isinstance(resolution, str) and resolution else ""
        return (
            f"List events on {arguments.get('calendar_id')}\n"
            f"Window: {arguments.get('time_min')} to {arguments.get('time_max')}\n"
            f"{resolved}"
            f"Max items {limits.get('max_items')}. Google is contacted only after approval."
        )
    if tool == "calendar.get_event":
        return (
            f"Get event {arguments.get('event_id')} on {arguments.get('calendar_id')}. "
            "Google is contacted only after approval."
        )
    if tool == "calendar.create_event":
        return "Create event\n" + _change_block(arguments)
    if tool == "calendar.update_event":
        return (
            "Update event\n"
            f"Calendar: {arguments.get('calendar_id')}\n"
            f"Event id: {arguments.get('event_id')}\n"
            + _notify_line(str(arguments.get("send_updates") or "none"))
            + "\nBefore:\n"
            + _change_block(arguments.get("before") or {})
            + "\nAfter:\n"
            + _change_block(arguments.get("after") or {})
        )
    if tool == "calendar.delete_event":
        return "DESTRUCTIVE: delete this event.\n" + _change_block(arguments)
    return "Read-only Google fetch."


def event_resource(fields: dict, event_id: str) -> dict:
    return {
        "id": event_id,
        "summary": fields.get("title") or "",
        "description": fields.get("description") or "",
        "location": fields.get("location") or "",
        "start": {"dateTime": fields.get("start") or "", "timeZone": fields.get("time_zone") or ""},
        "end": {"dateTime": fields.get("end") or "", "timeZone": fields.get("time_zone") or ""},
        "attendees": [{"email": item} for item in fields.get("attendees") or []],
    }


def snapshot_from_google(event: dict) -> dict:
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    end = event.get("end") if isinstance(event.get("end"), dict) else {}
    attendees = []
    for item in event.get("attendees") or []:
        if isinstance(item, dict) and item.get("email"):
            attendees.append(str(item["email"]))
    return {
        "title": str(event.get("summary") or ""),
        "description": str(event.get("description") or ""),
        "location": str(event.get("location") or ""),
        "start": str(start.get("dateTime") or ""),
        "end": str(end.get("dateTime") or ""),
        "time_zone": str(start.get("timeZone") or ""),
        "attendees": sorted(attendees),
    }


def event_snapshot_suffix(event: dict) -> str:
    """One machine-readable snapshot on the same result line the user can read."""
    view = snapshot_from_google(event)
    payload = {"id": str(event.get("id") or ""), **view}
    return " || event-snapshot " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_event_snapshots(text: str) -> list[dict]:
    found = []
    marker = " || event-snapshot "
    for line in str(text or "").splitlines():
        if marker not in line:
            continue
        try:
            payload = json.loads(line.split(marker, 1)[1])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("id") and payload.get("start") and payload.get("end"):
            found.append(payload)
    return found


def same_snapshot(current: dict, expected: dict) -> bool:
    keys = ("title", "description", "location", "start", "end", "time_zone", "attendees")
    left = dict(current)
    right = dict(expected)
    left["attendees"] = sorted(str(item) for item in left.get("attendees") or [])
    right["attendees"] = sorted(str(item) for item in right.get("attendees") or [])
    return all(left.get(key) == right.get(key) for key in keys)


def op_name(tool: str) -> str:
    return _OPS.get(tool, "read")


def _change_block(fields: dict) -> str:
    attendees = ", ".join(fields.get("attendees") or []) or "(none)"
    lines = []
    if fields.get("calendar_id"):
        lines.append(f"Calendar: {fields['calendar_id']}")
    if fields.get("event_id"):
        lines.append(f"Event id: {fields['event_id']}")
    lines.extend(
        [
            f"Title: {fields.get('title') or ''}",
            f"Start: {fields.get('start') or ''}",
            f"End: {fields.get('end') or ''}",
            f"Time zone: {fields.get('time_zone') or ''}",
            f"Location: {fields.get('location') or ''}",
            f"Attendees: {attendees}",
        ]
    )
    if fields.get("description"):
        lines.append(f"Description: {fields['description']}")
    if fields.get("send_updates"):
        lines.append(_notify_line(str(fields["send_updates"])))
    return "\n".join(lines)


def _notify_line(send_updates: str) -> str:
    if send_updates == "none":
        return "Notifications: none. Attendees are not emailed."
    return f"Notifications: sendUpdates={send_updates}. This will email attendees."


def _calendar_id(arguments: dict, limits: dict) -> str:
    value = arguments.get("calendar_id") or limits.get("calendar_id") or ""
    text = str(value).strip()
    if not text or len(text) > 256 or any(char in text for char in "/\\\n\r "):
        raise SsiosError("connector_grant_denied", "calendar id is required", 403)
    return text


def _event_id(value: object, *, generate: bool = False) -> str:
    if value in (None, "") and generate:
        return "ev" + secrets.token_hex(16)
    text = str(value or "")
    if len(text) < 5 or len(text) > 1024 or any(char not in "0123456789abcdefghijklmnopqrstuv" for char in text):
        raise SsiosError("connector_grant_denied", "event id rejected", 403)
    return text


def _send_updates(value: object) -> str:
    if value in (None, ""):
        return "none"
    text = str(value)
    if text not in _SEND:
        raise SsiosError("connector_grant_denied", "sendUpdates rejected", 400)
    return text


def _snapshot(value: object, *, required: bool) -> dict:
    if not isinstance(value, dict):
        raise SsiosError("connector_grant_denied", "event fields rejected", 400)
    title = _bounded_text(value.get("title"), "title", 300, required=required)
    start = _timed(value.get("start"), "start")
    end = _timed(value.get("end"), "end")
    zone = _bounded_text(value.get("time_zone"), "time zone", 80, required=True)
    if _parse(start, "connector_grant_denied") >= _parse(end, "connector_grant_denied"):
        raise SsiosError("connector_grant_denied", "event end must be after the start", 400)
    return {
        "title": title,
        "description": _bounded_text(value.get("description"), "description", 4000, required=False),
        "location": _bounded_text(value.get("location"), "location", 300, required=False),
        "start": start,
        "end": end,
        "time_zone": zone,
        "attendees": _attendees(value.get("attendees")),
    }


def _window(start: object, end: object) -> tuple[str, str]:
    if not start or not end:
        raise SsiosError("connector_window_denied", "calendar list needs a start and end", 400)
    opened = _parse(str(start), "connector_window_denied")
    closed = _parse(str(end), "connector_window_denied")
    span = closed - opened
    if span <= timedelta(0) or span > MAX_WINDOW:
        raise SsiosError("connector_window_denied", "calendar window must be from 1 second through 31 days", 400)
    return str(start), str(end)


def _parse(value: str, code: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SsiosError(code, "calendar time must be RFC3339 with a time zone", 400) from exc
    if parsed.tzinfo is None:
        raise SsiosError(code, "calendar time must include a time zone", 400)
    return parsed.astimezone(timezone.utc)


def _timed(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 40 or any(char in text for char in "\n\r "):
        raise SsiosError("connector_grant_denied", f"event {label} rejected", 400)
    _parse(text, "connector_grant_denied")
    return text


def _bounded_text(value: object, label: str, limit: int, *, required: bool) -> str:
    text = "" if value is None else str(value)
    if any(char in text for char in "\n\r"):
        raise SsiosError("connector_grant_denied", f"event {label} rejected", 400)
    if required and not text.strip():
        raise SsiosError("connector_grant_denied", f"event {label} is required", 400)
    if len(text) > limit:
        raise SsiosError("connector_grant_denied", f"event {label} is too long", 400)
    return text.strip()


def _attendees(value: object) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > 20:
        raise SsiosError("connector_grant_denied", "attendees rejected", 400)
    emails = []
    for item in value:
        text = str(item).strip()
        if not text or len(text) > 254 or "@" not in text or any(char in text for char in " \n\r,"):
            raise SsiosError("connector_grant_denied", "attendee rejected", 400)
        emails.append(text)
    return emails

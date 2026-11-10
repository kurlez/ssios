"""Natural-language calendar intents.

The local model only fills a relative JSON object. This module checks that
object and turns it into absolute times. Date arithmetic stays here, using the
account time zone and the clock, so a wrong model date cannot reach an
approval card.

Week rules, Monday 00:00 through the next Monday 00:00 in the account zone:
- this week is the Monday-Sunday week that contains today
- next week, and 下周三 / next Wednesday, is the following week
- a bare weekday such as 周五 or Friday is the next one, and today counts
  when today is that weekday
- if that clock time has already passed, the card still uses today and says so

A read with no range is today 00:00 through seven days, still inside the
31-day cap. Names without an email stay in the title and description.
send_updates stays none unless the user's own words ask to notify attendees.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ssios.errors import FlowInterrupt, SsiosError

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_RANGES = {"today", "tomorrow", "this_week", "next_week", "tomorrow_afternoon", "weekday", "unspecified"}
_PARTS = {"", "morning", "afternoon", "evening"}
_OPS = {"list", "create", "update", "delete"}
_KEYS = {
    "op",
    "range_kind",
    "weekday",
    "weekday_scope",
    "hour",
    "minute",
    "duration_minutes",
    "part_of_day",
    "title",
    "match_text",
    "location",
    "attendee_names",
    "attendee_emails",
    "notify",
    "calendar_id",
}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LEGACY = re.compile(
    r"(?:^|\s)(?:title|from|to|start|end|event|location|description|attendees|send|zone|calendar|"
    r"before_title|after_title|before_start|after_start|before_end|after_end|before_zone|after_zone):"
)
_LEGACY_COMMANDS = (
    " list my calendars",
    " list calendars",
    " list calendar",
    " get calendar event",
    " get event",
    " delete calendar event",
    " update calendar event",
    " create calendar event",
)
_NOTIFY_ALL = (
    "notify the attendees",
    "notify attendees",
    "email the attendees",
    "email attendees",
    "send updates",
    "send an email",
    "通知参会",
    "通知他们",
    "发邮件",
)
FALLBACK = (
    "The local model could not read that calendar request. Nothing was read or changed. "
    "You can say it again, or use the explicit form, for example: "
    "list calendar events calendar:primary from:2026-11-10T00:00:00Z to:2026-11-17T00:00:00Z"
)
INTENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(_KEYS),
    "properties": {
        "op": {"type": "string", "enum": ["list", "create", "update", "delete"]},
        "range_kind": {"type": "string", "enum": sorted(_RANGES)},
        "weekday": {"type": "string", "enum": ["", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
        "weekday_scope": {"type": "string", "enum": ["", "this", "next", "upcoming"]},
        "hour": {"type": "integer"},
        "minute": {"type": "integer"},
        "duration_minutes": {"type": "integer"},
        "part_of_day": {"type": "string", "enum": ["", "morning", "afternoon", "evening"]},
        "title": {"type": "string"},
        "match_text": {"type": "string"},
        "location": {"type": "string"},
        "attendee_names": {"type": "array", "items": {"type": "string"}},
        "attendee_emails": {"type": "array", "items": {"type": "string"}},
        "notify": {"type": "string", "enum": ["none", "all", "externalOnly"]},
        "calendar_id": {"type": "string"},
    },
}
_INTENT_SYSTEM = (
    "You extract one calendar intent as a JSON object. Do not calculate a calendar date. "
    "Do not invent an email address. Hour is 0-23 or -1 when the user gave no clock time. "
    "下午三点 is hour 15. 下午四点 is hour 16. 一小时 is duration_minutes 60. "
    "range_kind is today, tomorrow, this_week, next_week, tomorrow_afternoon, weekday, or unspecified. "
    "下周三 or next Wednesday is weekday wed and weekday_scope next. "
    "A bare 周五 or Friday is weekday fri and weekday_scope upcoming. "
    "notify is none unless the user explicitly asks to email or notify attendees. "
    "calendar_id is primary. attendee_names holds names that are not emails."
)


def mentions_calendar(message: str) -> bool:
    folded = message.casefold()
    if "calendar" in folded:
        return True
    if any(cue in message for cue in ("日历", "日程", "安排", "有空", "开会", "会议", "站会", "牙医")):
        return True
    if "改到" in message and any(part in message for part in ("点", "上午", "下午", "早上", "晚上")):
        return True
    if "取消" in message and any(part in message for part in ("会", "安排", "日程", "日历")):
        return True
    return False


def is_legacy_calendar(message: str) -> bool:
    if _LEGACY.search(message):
        return True
    folded = " " + message.casefold() + " "
    return any(token in folded for token in _LEGACY_COMMANDS)


def account_zone(settings):
    name = str(getattr(settings, "ui_time_zone", "") or "")
    if name:
        try:
            return ZoneInfo(name), name
        except ZoneInfoNotFoundError as exc:
            raise SsiosError("config_invalid", "ui time_zone must be an IANA time zone name", 500) from exc
    current = datetime.now().astimezone().tzinfo
    key = str(getattr(current, "key", "") or "")
    return current, key or "local"


def clock_now(plane, zone) -> datetime:
    raw = getattr(plane, "calendar_now", None)
    if raw is None:
        return datetime.now(zone)
    if not isinstance(raw, datetime):
        raise SsiosError("config_invalid", "calendar clock is invalid", 500)
    if raw.tzinfo is None:
        raw = raw.replace(tzinfo=timezone.utc)
    return raw.astimezone(zone)


def intent_messages(message: str, *, repair: bool) -> list[dict]:
    system = _INTENT_SYSTEM
    if repair:
        system += " The previous answer was not valid JSON. Return only the object."
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": message},
    ]


def extract_intent(llm, message: str, deadline: float) -> dict:
    reader = getattr(llm, "extract_calendar_intent", None)
    if not callable(reader):
        raise SsiosError("calendar_intent_invalid", FALLBACK, 400)
    first_error = _read_once(reader, message, deadline, repair=False)
    if isinstance(first_error, dict):
        return first_error
    if first_error == "unavailable":
        raise SsiosError("calendar_intent_invalid", FALLBACK, 400)
    second = _read_once(reader, message, deadline, repair=True)
    if isinstance(second, dict):
        return second
    raise SsiosError("calendar_intent_invalid", FALLBACK, 400)


def plan_calendar(message: str, *, now: datetime, zone_name: str, llm, deadline: float) -> tuple[str, dict, dict | None]:
    intent = extract_intent(llm, message, deadline)
    return plan_from_intent(intent, message, now, zone_name)


def plan_from_intent(intent: dict, message: str, now: datetime, zone_name: str) -> tuple[str, dict, dict | None]:
    if now.tzinfo is None:
        raise SsiosError("config_invalid", "calendar clock must include a time zone", 500)
    cleaned = _ground(intent, message)
    op = cleaned["op"]
    send_updates = _notify_from_user(message)
    window_start, window_end, moment, note = _resolve_when(cleaned, now, zone_name)
    names = cleaned["attendee_names"]
    emails = cleaned["attendee_emails"]
    title = cleaned["title"]
    if names and (not title or names[0] not in title):
        title = f"{title}（{names[0]}）" if title else names[0]
    description = _description(names, note)
    if op == "create":
        if moment is None:
            raise SsiosError("calendar_intent_unclear", "That request needs a clock time. Nothing was changed.", 400)
        end = moment + timedelta(minutes=cleaned["duration_minutes"] or 60)
        return "calendar.create_event", {
            "calendar_id": "primary",
            "title": title or "Calendar event",
            "start": _stamp(moment),
            "end": _stamp(end),
            "time_zone": zone_name,
            "location": cleaned["location"],
            "description": description,
            "attendees": emails,
            "send_updates": send_updates,
        }, None
    arguments = {
        "calendar_id": "primary",
        "time_min": _stamp(window_start),
        "time_max": _stamp(window_end),
        "resolution": note,
    }
    if op == "list":
        return "calendar.list_events", arguments, None
    followup = {
        "op": op,
        "match_text": cleaned["match_text"],
        "calendar_id": "primary",
        "send_updates": send_updates,
        "hour": cleaned["hour"],
        "minute": cleaned["minute"],
        "duration_minutes": cleaned["duration_minutes"],
        "day": (moment or window_start).date().isoformat(),
        "zone": zone_name,
        "title": title,
        "location": cleaned["location"],
        "note": note,
        "attendees": emails,
    }
    return "calendar.list_events", arguments, followup


def scripted_intent(message: str) -> dict:
    """Fake local model used by tests. It returns relative fields only."""
    intent = _blank()
    if "这周我有什么安排" in message:
        intent["op"] = "list"
        intent["range_kind"] = "this_week"
        return intent
    if "明天下午有空" in message:
        intent["op"] = "list"
        intent["range_kind"] = "tomorrow_afternoon"
        return intent
    if "下周三" in message and "张三" in message:
        intent.update(
            {
                "op": "create",
                "range_kind": "weekday",
                "weekday": "wed",
                "weekday_scope": "next",
                "hour": 15,
                "minute": 0,
                "duration_minutes": 60,
                "part_of_day": "afternoon",
                "title": "开会",
                "attendee_names": ["张三"],
            }
        )
        return intent
    if "牙医" in message:
        intent.update(
            {
                "op": "update",
                "range_kind": "weekday",
                "weekday": "fri",
                "weekday_scope": "upcoming",
                "hour": 16,
                "minute": 0,
                "part_of_day": "afternoon",
                "match_text": "牙医",
            }
        )
        return intent
    if "取消" in message and "站会" in message:
        intent.update({"op": "delete", "range_kind": "tomorrow", "match_text": "站会"})
        return intent
    if "next week" in message.casefold() and "calendar" in message.casefold():
        intent["op"] = "list"
        intent["range_kind"] = "next_week"
        return intent
    intent["op"] = "list"
    intent["range_kind"] = "unspecified"
    return intent


def _read_once(reader, message: str, deadline: float, *, repair: bool):
    try:
        raw = reader(message, deadline=deadline, repair=repair)
    except FlowInterrupt:
        return "unavailable"
    except SsiosError as exc:
        if exc.code in {"llm_unavailable", "timeout", "model_format_unsupported", "ollama_rejected"}:
            return "unavailable"
        return "invalid"
    try:
        return _validate(raw)
    except (SsiosError, TypeError, ValueError):
        return "invalid"


def _validate(raw: object) -> dict:
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SsiosError("calendar_intent_invalid", FALLBACK, 400) from exc
    if not isinstance(raw, dict) or set(raw) != _KEYS:
        raise SsiosError("calendar_intent_invalid", FALLBACK, 400)
    if raw["op"] not in _OPS or raw["range_kind"] not in _RANGES:
        raise SsiosError("calendar_intent_invalid", FALLBACK, 400)
    if raw["weekday"] not in {""} | set(_WEEKDAYS) or raw["weekday_scope"] not in {"", "this", "next", "upcoming"}:
        raise SsiosError("calendar_intent_invalid", FALLBACK, 400)
    if raw["part_of_day"] not in _PARTS or raw["notify"] not in {"none", "all", "externalOnly"}:
        raise SsiosError("calendar_intent_invalid", FALLBACK, 400)
    for key in ("hour", "minute", "duration_minutes"):
        if type(raw[key]) is not int:
            raise SsiosError("calendar_intent_invalid", FALLBACK, 400)
    if not -1 <= raw["hour"] <= 23 or not 0 <= raw["minute"] <= 59 or not 0 <= raw["duration_minutes"] <= 1440:
        raise SsiosError("calendar_intent_invalid", FALLBACK, 400)
    for key in ("title", "match_text", "location", "calendar_id"):
        if not isinstance(raw[key], str) or len(raw[key]) > 300:
            raise SsiosError("calendar_intent_invalid", FALLBACK, 400)
    for key in ("attendee_names", "attendee_emails"):
        values = raw[key]
        if not isinstance(values, list) or len(values) > 5 or any(not isinstance(item, str) or len(item) > 254 for item in values):
            raise SsiosError("calendar_intent_invalid", FALLBACK, 400)
    if raw["range_kind"] == "weekday" and raw["weekday"] not in _WEEKDAYS:
        raise SsiosError("calendar_intent_invalid", FALLBACK, 400)
    return raw


def _ground(intent: dict, message: str) -> dict:
    cleaned = dict(intent)
    cleaned["title"] = intent["title"].strip() if intent["title"].strip() and intent["title"].strip() in message else ""
    cleaned["match_text"] = intent["match_text"].strip() if intent["match_text"].strip() and intent["match_text"].strip() in message else ""
    cleaned["location"] = intent["location"].strip() if intent["location"].strip() and intent["location"].strip() in message else ""
    names = []
    for name in intent["attendee_names"]:
        text = name.strip()
        if text and "@" not in text and text in message and text not in names:
            names.append(text)
    emails = []
    for email in intent["attendee_emails"]:
        text = email.strip()
        if text and _EMAIL.fullmatch(text) and text in message and text not in emails:
            emails.append(text)
    cleaned["attendee_names"] = names
    cleaned["attendee_emails"] = emails
    hour = intent["hour"]
    if intent["part_of_day"] in {"afternoon", "evening"} and 0 <= hour <= 11:
        hour += 12
    cleaned["hour"] = hour
    if cleaned["range_kind"] == "weekday" and not cleaned["weekday_scope"]:
        cleaned["weekday_scope"] = "upcoming"
    return cleaned


def _notify_from_user(message: str) -> str:
    folded = message.casefold()
    if "external only" in folded or "externalonly" in folded or "仅通知外部" in message:
        return "externalOnly"
    if any(cue in folded or cue in message for cue in _NOTIFY_ALL):
        return "all"
    return "none"


def _resolve_when(intent: dict, now: datetime, zone_name: str):
    zone = now.tzinfo
    kind = intent["range_kind"]
    today = now.date()
    if kind == "weekday":
        scope = intent["weekday_scope"] or "upcoming"
        target = _weekday_date(today, intent["weekday"], scope)
        start = _at(target, zone)
        end = start + timedelta(days=1)
        moment = None
        if intent["hour"] >= 0:
            moment = start.replace(hour=intent["hour"], minute=intent["minute"])
        note = _weekday_note(moment or start, today, target, scope, zone_name, now)
        return start, end, moment, note
    if kind == "tomorrow_afternoon":
        start = _at(today + timedelta(days=1), zone).replace(hour=13)
        end = start.replace(hour=17)
        return start, end, None, f"Resolved: { _day_text(start) } 13:00 to 17:00 {zone_name}. Tomorrow afternoon."
    if kind == "tomorrow":
        start = _at(today + timedelta(days=1), zone)
        end = start + timedelta(days=1)
        return start, end, None, f"Resolved: { _day_text(start) } {zone_name}. Tomorrow."
    if kind == "today":
        start = _at(today, zone)
        end = start + timedelta(days=1)
        return start, end, None, f"Resolved: { _day_text(start) } {zone_name}. Today."
    if kind == "this_week":
        start = _at(_monday(today), zone)
        end = start + timedelta(days=7)
        return start, end, None, (
            f"Resolved: { _day_text(start) } 00:00 to { _day_text(end) } 00:00 {zone_name}. "
            "This week is Monday 00:00 through the next Monday 00:00."
        )
    if kind == "next_week":
        start = _at(_monday(today) + timedelta(days=7), zone)
        end = start + timedelta(days=7)
        return start, end, None, (
            f"Resolved: { _day_text(start) } 00:00 to { _day_text(end) } 00:00 {zone_name}. "
            "Next week is the following Monday 00:00 through the Monday after that."
        )
    start = _at(today, zone)
    end = start + timedelta(days=7)
    return start, end, None, (
        f"Resolved: { _day_text(start) } 00:00 to { _day_text(end) } 00:00 {zone_name}. "
        "No date was given, so this is today through seven days."
    )


def _weekday_date(today, weekday: str, scope: str):
    index = _WEEKDAYS[weekday]
    monday = _monday(today)
    if scope == "next":
        return monday + timedelta(days=7 + index)
    if scope == "this":
        return monday + timedelta(days=index)
    return today + timedelta(days=(index - today.weekday()) % 7)


def _weekday_note(moment: datetime, today, target, scope: str, zone_name: str, now: datetime) -> str:
    when = moment.strftime("%Y-%m-%d %H:%M")
    if scope == "next":
        return (
            f"Resolved: {when} {zone_name}. "
            "This is that weekday in the next calendar week (Monday through Sunday)."
        )
    if target == today:
        passed = " This time has already passed." if moment <= now else ""
        return f"Resolved: {when} {zone_name}. Today is that weekday, so this is today.{passed}"
    return f"Resolved: {when} {zone_name}. This is the next {moment.strftime('%A')}."


def _description(names: list[str], note: str) -> str:
    parts = []
    if names:
        parts.append("With: " + ", ".join(names))
    if note:
        parts.append(note)
    return ". ".join(parts)


def _monday(day):
    return day - timedelta(days=day.weekday())


def _at(day, zone) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=zone)


def _stamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _day_text(value: datetime) -> str:
    return value.date().isoformat()


def _blank() -> dict:
    return {
        "op": "list",
        "range_kind": "unspecified",
        "weekday": "",
        "weekday_scope": "",
        "hour": -1,
        "minute": 0,
        "duration_minutes": 0,
        "part_of_day": "",
        "title": "",
        "match_text": "",
        "location": "",
        "attendee_names": [],
        "attendee_emails": [],
        "notify": "none",
        "calendar_id": "primary",
    }


def deadline_from(settings) -> float:
    return time.monotonic() + float(getattr(settings, "llm_timeout_seconds", 60) or 60)

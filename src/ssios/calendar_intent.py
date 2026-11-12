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
_RANGES = {"today", "tomorrow", "day_after", "this_week", "next_week", "tomorrow_afternoon", "weekday", "unspecified"}
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


_CREATIVE = re.compile(
    r"(帮我写|写一个|写一篇|写首|写一首|小故事|故事|诗|文章|总结一下什么是|假如|想象|假设|会怎样|"
    r"\b(write|story|poem|essay|explain|imagine|suppose|hypothetical)\b|what if)",
    re.IGNORECASE,
)
_GENERAL = re.compile(r"^(what is|what's an|what's a|explain|define)\b", re.IGNORECASE)
_CLOCK = re.compile(r"(点|点钟|\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm)\b|\bat\s+\d{1,2}\b)", re.IGNORECASE)
_CAL_VERB = re.compile(
    r"(加到日历|添加到日历|创建会议|创建个会议|新建.{0,8}(?:会|会议)|安排.{0,12}(?:会|会议)|(?<!大)约)",
)
_CAL_VERB_EN = re.compile(
    r"\b(?:schedule|book|add to (?:my )?calendar|put on (?:my )?calendar|create (?:a )?(?:meeting|event))\b",
    re.IGNORECASE,
)


def creative_request(message: str) -> bool:
    """Writing, a story, or a hypothetical. A meeting word inside it is not a calendar change."""
    return bool(_CREATIVE.search(" ".join(str(message or "").split())))


def general_question(message: str) -> bool:
    """A definition, not a request to read the user's mail, calendar, or weather."""
    text = " ".join(str(message or "").split())
    if any(cue in text for cue in ("什么是", "是什么意思", "是啥")):
        return not any(part in text for part in ("我的", "我有", "查一下", "查我"))
    if _GENERAL.search(text):
        if re.search(r"\b(price|prices|weather|forecast|news)\b", text, re.IGNORECASE):
            return False
        return not re.search(r"\b(my|inbox)\b", text, re.IGNORECASE)
    return False


def _has_clock(text: str) -> bool:
    return bool(_CLOCK.search(text))


def calendar_write(message: str) -> bool:
    """A change to the user's own calendar needs an action and an event, or a clock time plus a meeting."""
    text = " ".join(str(message or "").split())
    if not text or creative_request(text) or general_question(text):
        return False
    folded = text.casefold()
    verb = bool(_CAL_VERB.search(text) or _CAL_VERB_EN.search(folded))
    meeting = bool(re.search(r"(开会|会议|见面|meeting|appointment)", text, re.IGNORECASE))
    if verb and (meeting or _has_clock(text)):
        return True
    return bool(_has_clock(text) and meeting)


def mentions_calendar(message: str) -> bool:
    text = " ".join(str(message or "").split())
    if creative_request(text) or general_question(text):
        return False
    folded = text.casefold()
    if "calendar" in folded or any(cue in text for cue in ("日历", "日程", "有空", "什么会", "什么安排", "有什么安排", "站会", "牙医")):
        return True
    if calendar_write(text):
        return True
    if "改到" in text and any(part in text for part in ("点", "上午", "下午", "早上", "晚上")):
        return True
    if "取消" in text and any(part in text for part in ("会", "安排", "日程", "日历")):
        return True
    return False


def is_legacy_calendar(message: str) -> bool:
    if _LEGACY.search(message):
        return True
    folded = " " + message.casefold() + " "
    return any(token in folded for token in _LEGACY_COMMANDS)


def account_zone(settings, account: dict | None = None):
    name = ""
    if isinstance(account, dict):
        name = str(account.get("time_zone") or "")
    if not name:
        name = str(getattr(settings, "ui_time_zone", "") or "")
    if name:
        try:
            return ZoneInfo(name), name
        except ZoneInfoNotFoundError as exc:
            raise SsiosError("config_invalid", "time_zone must be an IANA time zone name", 500) from exc
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


def host_clock(plane, account: dict | None = None) -> tuple[datetime, str]:
    """The one clock for chat context and for relative dates."""
    stored = account
    users = getattr(getattr(plane, "accounts", None), "users", None)
    if isinstance(account, dict) and isinstance(users, dict):
        found = users.get(str(account.get("id") or ""))
        if isinstance(found, dict):
            stored = found
    zone, zone_name = account_zone(plane.settings, stored if isinstance(stored, dict) else None)
    return clock_now(plane, zone), zone_name


def clock_block(plane, account: dict | None = None) -> str:
    now, zone_name = host_clock(plane, account)
    stamp = now.replace(microsecond=0).isoformat()
    return (
        "HOST-CLOCK\n"
        f"Time zone: {zone_name}\n"
        f"Local date: {now.strftime('%A')}, {now.day} {now.strftime('%B %Y')}\n"
        f"Local time: {now.strftime('%H:%M')}\n"
        f"ISO: {stamp}\n"
        "Never invent a date, a weekday, or a time. Use this clock."
    )


def clock_answer(message: str, now: datetime, zone_name: str) -> str:
    """A pure date or time question. Empty when the model should answer."""
    text = " ".join(str(message or "").split())
    kind = _clock_kind(text)
    if not kind:
        return ""
    iso = now.replace(microsecond=0).isoformat()
    if kind == "time":
        return f"The local time is {now.strftime('%H:%M')} ({iso}) in {zone_name}."
    if kind == "weekday":
        return f"Today is {now.strftime('%A')} in {zone_name}."
    day = now
    if kind == "tomorrow":
        day = now + timedelta(days=1)
    elif kind == "day_after":
        day = now + timedelta(days=2)
    elif kind == "yesterday":
        day = now - timedelta(days=1)
    label = {"today": "Today", "tomorrow": "Tomorrow", "day_after": "The day after tomorrow", "yesterday": "Yesterday"}[kind]
    return f"{label} is {day.strftime('%A')}, {day.day} {day.strftime('%B %Y')}, in {zone_name} ({day.replace(microsecond=0).isoformat()})."


def _clock_kind(text: str) -> str:
    folded = text.casefold().rstrip("？?").strip()
    phrases = {
        "今天的日期": "today",
        "今天几号": "today",
        "今天是几号": "today",
        "今天是什么日期": "today",
        "今天星期几": "weekday",
        "今天周几": "weekday",
        "今天是星期几": "weekday",
        "今天是周几": "weekday",
        "现在几点": "time",
        "现在几点了": "time",
        "现在几号": "today",
        "明天几号": "tomorrow",
        "明天的日期": "tomorrow",
        "后天几号": "day_after",
        "昨天几号": "yesterday",
    }
    if text.rstrip("？?").strip() in phrases:
        return phrases[text.rstrip("？?").strip()]
    english = {
        "what is the date": "today",
        "what's the date": "today",
        "whats the date": "today",
        "what is today's date": "today",
        "what's today's date": "today",
        "what is the date today": "today",
        "what day is it": "weekday",
        "what day is today": "weekday",
        "what is the time": "time",
        "what's the time": "time",
        "what time is it": "time",
        "what is tomorrow's date": "tomorrow",
        "what's tomorrow's date": "tomorrow",
        "what is tomorrow": "tomorrow",
        "what day is tomorrow": "tomorrow",
    }
    return english.get(folded, "")


def intent_messages(message: str, *, repair: bool) -> list[dict]:
    system = _INTENT_SYSTEM + " Never invent a date or an ISO timestamp. The host clock computes every date."
    if repair:
        system += " The previous answer was not valid JSON. Return only the object."
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": message},
    ]


def extract_intent(llm, message: str, deadline: float) -> dict:
    """The model only fills slots. A common phrase still resolves when that JSON is bad."""
    phrase = fallback_intent(message)
    reader = getattr(llm, "extract_calendar_intent", None)
    if phrase is not None and not _needs_model(message):
        return phrase
    if not callable(reader):
        if phrase is not None:
            return phrase
        raise SsiosError("calendar_intent_invalid", intent_error(message), 400)
    first = _read_once(reader, message, deadline, repair=False)
    if isinstance(first, dict):
        return _merge_phrase(first, phrase)
    if first != "unavailable":
        second = _read_once(reader, message, deadline, repair=True)
        if isinstance(second, dict):
            return _merge_phrase(second, phrase)
    if phrase is not None:
        return phrase
    raise SsiosError("calendar_intent_invalid", intent_error(message), 400)


def intent_error(message: str) -> str:
    text = FALLBACK
    if re.search(r"[\u4e00-\u9fff]", message or ""):
        text += " For example: 查一下我明天有什么日程"
    return text


def _needs_model(message: str) -> bool:
    """Creates and edits still ask the model, then keep the phrase if that answer is unusable."""
    folded = message.casefold()
    if any(cue in message for cue in ("取消", "删除", "改到", "改成", "改为", "开会")):
        return True
    return bool(re.search(r"\b(cancel|delete|reschedule|move|schedule|create|book)\b", folded))


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
        if intent.get("op") == "list":
            start, end = _maybe_part(intent, start, end)
        note = _weekday_note(moment or start, today, target, scope, zone_name, now)
        return start, end, moment, note
    if kind == "tomorrow_afternoon":
        start = _at(today + timedelta(days=1), zone).replace(hour=13)
        end = start.replace(hour=17)
        return start, end, None, f"Resolved: { _day_text(start) } 13:00 to 17:00 {zone_name}. Tomorrow afternoon."
    if kind == "tomorrow":
        start = _at(today + timedelta(days=1), zone)
        end = start + timedelta(days=1)
        start, end = _maybe_part(intent, start, end)
        return start, end, None, f"Resolved: { _day_text(start) } {zone_name}. Tomorrow."
    if kind == "day_after":
        start = _at(today + timedelta(days=2), zone)
        end = start + timedelta(days=1)
        start, end = _maybe_part(intent, start, end)
        return start, end, None, f"Resolved: { _day_text(start) } {zone_name}. The day after tomorrow."
    if kind == "today":
        start = _at(today, zone)
        end = start + timedelta(days=1)
        start, end = _maybe_part(intent, start, end)
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


def _maybe_part(intent: dict, start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if intent.get("op") not in {"", "list"}:
        return start, end
    part = intent.get("part_of_day") or ""
    hours = {"morning": (8, 12), "afternoon": (13, 17), "evening": (18, 22)}
    if part not in hours or intent.get("range_kind") == "tomorrow_afternoon":
        return start, end
    open_hour, close_hour = hours[part]
    begin = start.replace(hour=open_hour, minute=0, second=0, microsecond=0)
    finish = start.replace(hour=close_hour, minute=0, second=0, microsecond=0)
    if finish <= begin:
        return start, end
    return begin, finish


def _merge_phrase(parsed: dict, phrase: dict | None) -> dict:
    if not phrase or phrase.get("range_kind") in {None, "", "unspecified"}:
        return parsed
    if parsed.get("range_kind") not in {None, "", "unspecified"}:
        return parsed
    merged = dict(parsed)
    for key in ("range_kind", "weekday", "weekday_scope", "part_of_day", "hour", "minute", "duration_minutes", "op"):
        if phrase.get(key) not in (None, "", -1, 0) or key in {"weekday", "weekday_scope", "part_of_day", "op", "range_kind"}:
            merged[key] = phrase[key]
    return merged


def fallback_intent(message: str) -> dict | None:
    """Relative words such as 明天 and tomorrow. Dates are not computed here."""
    text = " ".join(str(message or "").split())
    if not text:
        return None
    folded = text.casefold()
    intent = _blank()
    intent["op"] = _phrase_op(text, folded)
    kind, weekday, scope = _phrase_range(text, folded)
    if kind is None:
        if intent["op"] == "list" and _list_cue(text, folded):
            kind = "unspecified"
        else:
            return None
    if intent["op"] != "list" and kind == "unspecified":
        return None
    intent["range_kind"] = kind
    intent["weekday"] = weekday
    intent["weekday_scope"] = scope
    part = _phrase_part(text, folded)
    intent["part_of_day"] = part
    if kind == "tomorrow" and part == "afternoon" and intent["op"] == "list" and "有空" in text:
        intent["range_kind"] = "tomorrow_afternoon"
    hour, minute = _phrase_clock(text, folded, part)
    intent["hour"] = hour
    intent["minute"] = minute
    intent["duration_minutes"] = _phrase_duration(text, folded)
    if intent["op"] in {"update", "delete"}:
        for token in ("牙医", "站会", "会议"):
            if token in text:
                intent["match_text"] = token
                break
    if intent["op"] == "create" and "开会" in text:
        intent["title"] = "开会"
    name = _phrase_name(text)
    if name:
        intent["attendee_names"] = [name]
    return intent


def _list_cue(text: str, folded: str) -> bool:
    if any(cue in text for cue in ("查", "看看", "有什么", "有哪些", "哪些", "有空", "什么日程", "什么安排", "什么会")):
        return True
    return bool(re.search(r"\b(what|what's|show|list|any)\b", folded))


def _phrase_op(text: str, folded: str) -> str:
    if _list_cue(text, folded):
        return "list"
    if any(cue in text for cue in ("取消", "删除")) or re.search(r"\b(cancel|delete)\b", folded):
        return "delete"
    if any(cue in text for cue in ("改到", "改成", "改为")) or re.search(r"\b(reschedule|move)\b", folded):
        return "update"
    if calendar_write(text) or any(cue in text for cue in ("新建", "预约")):
        return "create"
    return "list"


def _phrase_range(text: str, folded: str) -> tuple[str | None, str, str]:
    if "后天" in text or "day after tomorrow" in folded:
        return "day_after", "", ""
    if "明天" in text or re.search(r"\btomorrow\b", folded):
        return "tomorrow", "", ""
    if "今天" in text or "今儿" in text or re.search(r"\btoday\b", folded):
        return "today", "", ""
    found = re.search(r"(下个?|这个?|本)?(?:周|星期)([一二三四五六日天])", text)
    if found:
        weekday = _CN_DAY[found.group(2)]
        prefix = found.group(1) or ""
        scope = "next" if prefix.startswith("下") else "this" if prefix else "upcoming"
        return "weekday", weekday, scope
    english = re.search(
        r"\b(next|this)?\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        folded,
    )
    if english:
        names = {
            "monday": "mon",
            "tuesday": "tue",
            "wednesday": "wed",
            "thursday": "thu",
            "friday": "fri",
            "saturday": "sat",
            "sunday": "sun",
        }
        scope = english.group(1) or "upcoming"
        return "weekday", names[english.group(2)], scope
    if "下周" in text or "下个周" in text or "next week" in folded:
        return "next_week", "", ""
    if any(cue in text for cue in ("本周", "这周", "这个周")) or "this week" in folded:
        return "this_week", "", ""
    return None, "", ""


_CN_DAY = {"一": "mon", "二": "tue", "三": "wed", "四": "thu", "五": "fri", "六": "sat", "日": "sun", "天": "sun"}
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _phrase_part(text: str, folded: str) -> str:
    if "下午" in text or "afternoon" in folded:
        return "afternoon"
    if "上午" in text or "早上" in text or "morning" in folded:
        return "morning"
    if "晚上" in text or "evening" in folded:
        return "evening"
    return ""


def _phrase_clock(text: str, folded: str, part: str) -> tuple[int, int]:
    minute = 0
    match = re.search(r"([零一二两三四五六七八九十\d]{1,3})\s*点(半)?", text)
    if match:
        hour = _cn_number(match.group(1))
        if match.group(2):
            minute = 30
    else:
        match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", folded)
        if not match:
            return -1, 0
        hour = int(match.group(1))
        if match.group(2):
            minute = int(match.group(2))
        if match.group(3) == "pm" and hour < 12:
            hour += 12
        if match.group(3) == "am" and hour == 12:
            hour = 0
    if hour is None or not 0 <= hour <= 23:
        return -1, 0
    if part in {"afternoon", "evening"} and hour <= 11:
        hour += 12
    return hour, minute


def _cn_number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if len(token) == 2 and token[0] == "十" and token[1] in _CN_NUM:
        return 10 + _CN_NUM[token[1]]
    if len(token) == 2 and token[1] == "十" and token[0] in _CN_NUM:
        return _CN_NUM[token[0]] * 10
    if len(token) == 3 and token[1] == "十" and token[0] in _CN_NUM and token[2] in _CN_NUM:
        return _CN_NUM[token[0]] * 10 + _CN_NUM[token[2]]
    if len(token) == 1 and token in _CN_NUM:
        return _CN_NUM[token]
    return None


def _phrase_duration(text: str, folded: str) -> int:
    if "半小时" in text or "half an hour" in folded:
        return 30
    if "一个小时" in text or "一小时" in text or "an hour" in folded or "1 hour" in folded:
        return 60
    match = re.search(r"(\d+)\s*(?:个)?小时", text)
    if match:
        return min(1440, int(match.group(1)) * 60)
    match = re.search(r"(\d+)\s*分钟", text)
    if match:
        return min(1440, int(match.group(1)))
    match = re.search(r"\b(\d+)\s*hours?\b", folded)
    if match:
        return min(1440, int(match.group(1)) * 60)
    return 0


def _phrase_name(text: str) -> str:
    found = re.search(r"和([\u4e00-\u9fffA-Za-z]{1,12}?)(?:开会|见面)", text)
    if not found:
        return ""
    name = found.group(1).strip()
    if name in {"我", "他", "她"}:
        return ""
    return name


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

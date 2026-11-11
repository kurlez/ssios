"""Per-account reminders. Times are absolute when they are captured."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from ssios.calendar_intent import _phrase_clock, _phrase_part, _phrase_range
from ssios.util import read_json, write_json

_CUE = ("记一下", "记下", "记住", "提醒我", "帮我记")


def wants_remember(message: str) -> bool:
    folded = message.casefold()
    if re.search(r"\b(remember|remind me)\b", folded):
        return True
    if re.search(r"\bsave (this|that) to memory\b", folded):
        return True
    return any(cue in message for cue in _CUE)


def is_agenda(message: str) -> bool:
    """A question about what is planned, not a request to save a reminder."""
    if wants_remember(message) or wants_forget(message):
        return False
    text = str(message or "")
    if any(cue in text for cue in ("有什么事", "有什么事情", "需要做什么", "要做什么", "什么事情")):
        return True
    return bool(re.search(r"\b(what do i have|what do i need to do|what(?:'s| is) on today)\b", text.casefold()))


def wants_forget(message: str) -> bool:
    folded = message.casefold()
    if re.search(r"\bforget\b", folded):
        return True
    return any(cue in message for cue in ("忘记", "忘掉"))


def reminder_text(message: str) -> str:
    text = str(message or "").strip()
    text = re.sub(r"^(please\s+)?(remember(\s+to)?|remind me(?:\s+to)?|save (?:this|that) to memory)\b[:,\s]*", "", text, flags=re.IGNORECASE)
    for cue in ("帮我记一下", "帮我记住", "帮我记", "记一下", "记下", "记住", "提醒我"):
        text = text.replace(cue, " ")
    text = re.sub(r"(明天|今天|后天|上午|下午|晚上|早上)", " ", text)
    text = re.sub(r"([零一二两三四五六七八九十\d]{1,3})\s*点(半)?", " ", text)
    text = re.sub(r"\b(tomorrow|today|at|am|pm)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ，,。.")
    return text or str(message or "").strip()


def capture_due(message: str, now: datetime, zone) -> datetime | None:
    """Clock time in the message, resolved against ``now`` in ``zone``."""
    text = " ".join(str(message or "").split())
    folded = text.casefold()
    kind, _weekday, _scope = _phrase_range(text, folded)
    part = _phrase_part(text, folded)
    hour, minute = _phrase_clock(text, folded, part)
    if hour < 0:
        return None
    day = now.date()
    if kind == "tomorrow":
        day = day + timedelta(days=1)
    elif kind == "day_after":
        day = day + timedelta(days=2)
    elif kind in {None, "", "unspecified", "today"}:
        day = now.date()
    else:
        return None
    due = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    if kind in {None, "", "unspecified"} and due <= now:
        due = due + timedelta(days=1)
    return due.replace(microsecond=0)


def saved_line(due: datetime, zone_name: str, text: str) -> str:
    clock = due.strftime("%I:%M %p").lstrip("0")
    short = "PT" if zone_name == "America/Los_Angeles" else "ET" if "New_York" in zone_name else zone_name
    return f"Saved: {due.strftime('%a %b')} {due.day}, {clock} {short} — {text}"


class ReminderStore:
    def __init__(self, home) -> None:
        self.path = home / "reminders.json"

    def _load(self) -> list[dict]:
        data = read_json(self.path, {"items": []})
        items = data.get("items") if isinstance(data, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def _save(self, items: list[dict]) -> None:
        write_json(self.path, {"items": items})

    def add(self, *, text: str, phrase: str, due: datetime, zone_name: str, created: datetime) -> dict:
        import secrets

        items = self._load()
        item = {
            "id": "rem-" + secrets.token_hex(4),
            "text": text[:200],
            "phrase": phrase[:300],
            "due": due.replace(microsecond=0).isoformat(),
            "zone": zone_name,
            "created_at": created.replace(microsecond=0).isoformat(),
            "status": "open",
        }
        if any(cue in phrase for cue in ("每天", "every day", "daily")):
            item["repeat"] = "daily"
        items.append(item)
        self._save(items)
        self._mirror(items)
        return item

    def open_items(self) -> list[dict]:
        return [item for item in self._load() if item.get("status") == "open"]

    def between(self, start: datetime, end: datetime) -> list[dict]:
        found = []
        for item in self.open_items():
            try:
                due = datetime.fromisoformat(str(item.get("due") or ""))
            except ValueError:
                continue
            if start <= due < end:
                found.append(item)
        return found

    def complete_matching(self, needle: str) -> list[dict]:
        folded = needle.casefold()
        changed = []
        items = self._load()
        for item in items:
            if item.get("status") != "open":
                continue
            blob = (str(item.get("text") or "") + " " + str(item.get("phrase") or "")).casefold()
            if folded and folded in blob:
                item["status"] = "done"
                changed.append(item)
        if changed:
            self._save(items)
            self._mirror(items)
        return changed

    def _mirror(self, items: list[dict]) -> None:
        lines = ["# Tasks", ""]
        for item in items:
            if item.get("status") != "open":
                continue
            repeat = f" ({item['repeat']})" if item.get("repeat") else ""
            lines.append(f"- [ ] {item.get('due')} {item.get('text')}{repeat}")
        if len(lines) == 2:
            lines.append("- [ ] No open reminders.")
        (self.path.parent / "Tasks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

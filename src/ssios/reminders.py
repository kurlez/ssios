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
    text = re.sub(r"\b(remind me(?:\s+to)?|remember(?:\s+to)?)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(every day|daily)\b", " ", text, flags=re.IGNORECASE)
    for cue in ("帮我记一下", "帮我记住", "帮我记", "记一下", "记下", "记住", "提醒我", "每周一", "每星期一", "每周周一", "每天", "每日"):
        text = text.replace(cue, " ")
    text = re.sub(r"(明天|今天|后天|上午|下午|晚上|早上)", " ", text)
    text = re.sub(r"([零一二两三四五六七八九十\d]{1,3})\s*点(半)?", " ", text)
    text = re.sub(r"\b(tomorrow|today|at|am|pm)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ，,。.")
    text = re.sub(r"^(to|about)\s+", "", text, flags=re.IGNORECASE)
    return text or str(message or "").strip()


def reminder_command(message: str) -> str:
    """list, cancel, snooze, add, or empty when this is not a reminder command."""
    text = str(message or "")
    folded = text.casefold()
    if re.search(r"\b(list|show) my reminders\b", folded) or any(cue in text for cue in ("我的提醒", "列出提醒", "有哪些提醒")):
        return "list"
    if re.search(r"\b(snooze)\b", folded) or any(cue in text for cue in ("推迟", "稍后提醒")):
        return "snooze"
    if re.search(r"\bcancel\b.+\breminder\b|\bcancel the .+ reminder\b", folded) or "取消提醒" in text or "别提醒" in text:
        return "cancel"
    if "提醒我" in text or re.search(r"\bremind me\b", folded):
        return "add"
    return ""


def snooze_minutes(message: str) -> int:
    folded = str(message or "").casefold()
    found = re.search(r"(\d+)\s*(minute|minutes|hour|hours|分钟|小时)", folded)
    if not found:
        found = re.search(r"(\d+)\s*(分钟|小时)", str(message or ""))
    if not found:
        return 60
    count = int(found.group(1))
    unit = found.group(2)
    if unit.startswith("hour") or unit == "小时":
        return max(1, min(count, 24)) * 60
    return max(1, min(count, 24 * 60))


def reminder_schedule(message: str, now: datetime, zone) -> dict | None:
    """One absolute due time, plus daily or weekly when the words say so."""
    text = " ".join(str(message or "").split())
    folded = text.casefold()
    repeat = ""
    if any(cue in text for cue in ("每天", "每日")) or re.search(r"\bevery day\b|\bdaily\b", folded):
        repeat = "daily"
    weekly = re.search(r"\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", folded)
    if "每周一" in text or "每星期一" in text or "每周周一" in text:
        repeat = "weekly:mon"
    elif weekly:
        names = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"}
        repeat = "weekly:" + names[weekly.group(1)]
    if repeat.startswith("weekly:"):
        part = _phrase_part(text, folded)
        hour, minute = _phrase_clock(text, folded, part)
        if hour < 0:
            hour, minute = 9, 0
        due = _next_weekday(now, repeat.split(":", 1)[1], hour, minute, zone)
        return {"due": due, "repeat": repeat}
    due = capture_due(text, now, zone)
    if due is None:
        return None
    return {"due": due, "repeat": repeat}


def _next_weekday(now: datetime, code: str, hour: int, minute: int, zone) -> datetime:
    order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    target = order.index(code)
    day = now.date()
    for offset in range(0, 8):
        candidate_day = day + timedelta(days=offset)
        if candidate_day.weekday() != target:
            continue
        due = datetime(candidate_day.year, candidate_day.month, candidate_day.day, hour, minute, tzinfo=zone)
        if due > now:
            return due.replace(microsecond=0)
    return (now + timedelta(days=7)).replace(microsecond=0)


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


def sweep_accounts(plane) -> list[dict]:
    """Fire due reminders for every account. Safe to call on startup and on a timer."""
    from ssios.calendar_intent import host_clock

    root = plane.settings.state_dir / "accounts"
    if not root.is_dir():
        return []
    fired = []
    users = getattr(plane.accounts, "users", {}) if getattr(plane, "accounts", None) else {}
    for home in root.iterdir():
        if not home.is_dir() or not home.name.startswith("acct-"):
            continue
        account = users.get(home.name) if isinstance(users, dict) else None
        now, _zone = host_clock(plane, account if isinstance(account, dict) else {"id": home.name})
        fresh = ReminderStore(home).sweep(now)
        fired.extend(fresh)
        for notice in fresh:
            _notify_macos(str(notice.get("text") or "Reminder"))
    return fired


def _notify_macos(text: str) -> None:
    import sys

    if sys.platform != "darwin":
        return
    import shutil
    import subprocess

    if shutil.which("osascript") is None:
        return
    safe = text.replace("\\", " ").replace('"', " ")[:120]
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "SSIOS"'], check=False, timeout=3)


class ReminderStore:
    def __init__(self, home) -> None:
        self.path = home / "reminders.json"

    def _read(self) -> tuple[list[dict], list[dict]]:
        data = read_json(self.path, {"items": [], "notices": []})
        if not isinstance(data, dict):
            return [], []
        items = [item for item in data.get("items") or [] if isinstance(item, dict)]
        notices = [item for item in data.get("notices") or [] if isinstance(item, dict)]
        return items, notices

    def _load(self) -> list[dict]:
        items, _notices = self._read()
        return items

    def _write(self, items: list[dict], notices: list[dict]) -> None:
        write_json(self.path, {"items": items, "notices": notices[-40:]})

    def _save(self, items: list[dict]) -> None:
        _items, notices = self._read()
        del _items
        self._write(items, notices)

    def add(self, *, text: str, phrase: str, due: datetime, zone_name: str, created: datetime, task_id: str = "", repeat: str = "") -> dict:
        import secrets

        items, notices = self._read()
        item = {
            "id": "rem-" + secrets.token_hex(4),
            "text": text[:200],
            "phrase": phrase[:300],
            "due": due.replace(microsecond=0).isoformat(),
            "zone": zone_name,
            "created_at": created.replace(microsecond=0).isoformat(),
            "status": "open",
            "task_id": task_id,
            "repeat": repeat or ("daily" if any(cue in phrase for cue in ("每天", "every day", "daily")) else ""),
        }
        items.append(item)
        self._write(items, notices)
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

    def cancel_matching(self, needle: str) -> list[dict]:
        folded = needle.casefold()
        items, notices = self._read()
        changed = []
        for item in items:
            if item.get("status") != "open":
                continue
            blob = (str(item.get("text") or "") + " " + str(item.get("phrase") or "")).casefold()
            if folded and folded not in blob and folded not in str(item.get("id") or ""):
                continue
            item["status"] = "cancelled"
            changed.append(item)
        if changed:
            self._write(items, notices)
            self._mirror(items)
        return changed

    def cancel_for_task(self, task_id: str) -> list[dict]:
        if not task_id:
            return []
        items, notices = self._read()
        changed = []
        for item in items:
            if item.get("task_id") == task_id and item.get("status") == "open":
                item["status"] = "cancelled"
                changed.append(item)
        if changed:
            self._write(items, notices)
            self._mirror(items)
        return changed

    def snooze_matching(self, needle: str, minutes: int, now: datetime) -> list[dict]:
        folded = needle.casefold()
        items, notices = self._read()
        changed = []
        for item in items:
            if item.get("status") != "open":
                continue
            blob = (str(item.get("text") or "") + " " + str(item.get("phrase") or "")).casefold()
            if folded and folded not in blob and folded not in str(item.get("id")):
                continue
            try:
                due = datetime.fromisoformat(str(item.get("due") or ""))
            except ValueError:
                due = now
            if due.tzinfo is None and now.tzinfo is not None:
                due = due.replace(tzinfo=now.tzinfo)
            item["due"] = (max(due, now) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
            item["status"] = "open"
            changed.append(item)
            break
        if changed:
            self._write(items, notices)
        return changed

    def sweep(self, now: datetime) -> list[dict]:
        """Fire reminders whose time has passed. Recurring ones move forward. A task is not completed."""
        import secrets

        items, notices = self._read()
        fired = []
        changed = False
        for item in items:
            if item.get("status") != "open":
                continue
            try:
                due = datetime.fromisoformat(str(item.get("due") or ""))
            except ValueError:
                continue
            if due.tzinfo is None and now.tzinfo is not None:
                due = due.replace(tzinfo=now.tzinfo)
            if due > now:
                continue
            notice = {
                "id": "note-" + secrets.token_hex(3),
                "reminder_id": item.get("id"),
                "text": item.get("text") or "",
                "due": item.get("due") or "",
                "seen": False,
            }
            notices.append(notice)
            fired.append(notice)
            repeat = str(item.get("repeat") or "")
            if repeat == "daily" or repeat.startswith("weekly:"):
                step = timedelta(days=7 if repeat.startswith("weekly:") else 1)
                while due <= now:
                    due = due + step
                item["due"] = due.replace(microsecond=0).isoformat()
            else:
                item["status"] = "fired"
            changed = True
        if changed:
            self._write(items, notices)
            self._mirror(items)
        return fired

    def add_notice(self, text: str, when: datetime) -> dict:
        """A notice that is not tied to a reminder, such as the daily brief."""
        import secrets

        items, notices = self._read()
        notice = {"id": "note-" + secrets.token_hex(3), "reminder_id": "", "text": str(text)[:200], "due": when.replace(microsecond=0).isoformat(), "seen": False}
        notices.append(notice)
        self._write(items, notices)
        return notice

    def unseen(self) -> list[dict]:
        _items, notices = self._read()
        return [item for item in notices if not item.get("seen")]

    def mark_seen(self) -> None:
        items, notices = self._read()
        for item in notices:
            item["seen"] = True
        self._write(items, notices)

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

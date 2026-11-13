"""Work the host does on its timer without being asked. Both jobs are off until the account turns them on.

The daily brief is a list built from stored reminders, tasks, and a calendar
read. No model writes it, so it cannot invent an event. Analysis of new files
runs the same on-demand analysis, one file per tick, for a library whose owner
turned it on.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from ssios.errors import SsiosError
from ssios.util import read_json, write_json

_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DEFAULT_TIME = "08:00"


def brief_settings(home) -> dict:
    data = read_json(home / "brief.json", {})
    if not isinstance(data, dict):
        data = {}
    at = str(data.get("time") or DEFAULT_TIME)
    return {"enabled": bool(data.get("enabled")), "time": at if _CLOCK.match(at) else DEFAULT_TIME, "last": str(data.get("last") or "")}


def set_brief(home, *, enabled: bool, at: str) -> dict:
    if not _CLOCK.match(str(at or "")):
        raise SsiosError("brief_rejected", "The time must look like 08:00.", 400)
    current = brief_settings(home)
    current.update({"enabled": bool(enabled), "time": str(at)})
    write_json(home / "brief.json", current)
    return current


def compose_brief(plane, scope, now: datetime) -> str:
    """Today's reminders, tasks that are due or overdue, and calendar events. Stored data only."""
    from ssios.goals import GoalStore
    from ssios.reminders import ReminderStore

    zh = str(plane.skills.account_prefs(scope.account_id).get("language") or "") == "zh"
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    lines = [("今日简报 " if zh else "Daily brief ") + start.date().isoformat(), ""]
    reminders = ReminderStore(scope.home).between(start, end)
    lines.append(("提醒：" if zh else "Reminders: ") + str(len(reminders)))
    for item in reminders:
        lines.append(f"- {str(item.get('due') or '')[11:16]} {item.get('text') or ''}")
    due = []
    open_goals = GoalStore(scope.home).open_goals()
    for goal in open_goals:
        try:
            when = datetime.fromisoformat(str(goal.get("due") or ""))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=now.tzinfo)
        if when < end:
            due.append((when, goal))
    lines.append("")
    lines.append(("今天到期或已过期的任务：" if zh else "Tasks due today or overdue: ") + f"{len(due)}" + (f"（未完成共 {len(open_goals)} 项）" if zh else f" (of {len(open_goals)} open)"))
    for when, goal in sorted(due, key=lambda row: row[0]):
        lines.append(f"- {when.date().isoformat()} {goal.get('title') or ''}")
    lines.append("")
    lines.extend(_calendar_lines(plane, scope, start, end, zh))
    return "\n".join(lines).strip()


def _calendar_lines(plane, scope, start: datetime, end: datetime, zh: bool) -> list[str]:
    try:
        from ssios.google_connector import session_for_account

        session = session_for_account(scope.account)
        if not plane.google.status(session)["connectors"]["calendar"]["connected"]:
            return ["日历未连接。" if zh else "Calendar is not connected."]
        result = plane.google.read_now(
            session,
            project_id="notes",
            tool="calendar.list_events",
            arguments={"time_min": start.isoformat(), "time_max": end.isoformat()},
            limits={"max_items": 20, "max_bytes": 16000, "max_calls": 4},
        )
    except Exception:
        return ["日历这次没有读到。" if zh else "The calendar could not be read this time."]
    from ssios.google_calendar import parse_event_snapshots

    try:
        events = parse_event_snapshots(str(result.get("text") or ""))
    except Exception:
        events = []
    lines = [("日历：" if zh else "Calendar: ") + str(len(events))]
    for event in events:
        if isinstance(event, dict):
            lines.append(f"- {str(event.get('start') or '')[11:16]} {str(event.get('summary') or event.get('title') or '')[:120]}")
    return lines


def run_brief(plane, account: dict, now: datetime) -> str:
    """Post today's brief once, at or after the chosen time. Returns the conversation id, or empty."""
    from ssios.reminders import ReminderStore
    from ssios.scope import AccountScope

    scope = AccountScope(plane, account)
    settings = brief_settings(scope.home)
    today = now.date().isoformat()
    if not settings["enabled"] or settings["last"] == today or now.strftime("%H:%M") < settings["time"]:
        return ""
    settings["last"] = today
    write_json(scope.home / "brief.json", settings)
    text = compose_brief(plane, scope, now)
    title = text.splitlines()[0]
    store = scope.chat()
    record = store.create(title)
    store.append(record["id"], {"role": "assistant", "text": text, "route": {"route": "brief", "reason": "Built from stored reminders, tasks, and a calendar read. No model wrote it.", "source": "rule"}})
    ReminderStore(scope.home).add_notice(title, now)
    plane.security_audit.write(actor=scope.account_id, ip="127.0.0.1", action="brief.post", target=record["id"], result="ok")
    return record["id"]


def analyze_next(plane, account: dict, model=None) -> str:
    """Analyze one file that has no analysis yet, in a library this account owns and switched on. Returns the item id."""
    from ssios.library import _library_root, list_items, list_libraries, metadata
    from ssios.library_analyze import analyze_item

    account_id = str(account.get("id") or "")
    for row in list_libraries(plane, account_id):
        if row.get("kind") == "shared" and row.get("role") != "owner":
            continue
        library_id = str(row["id"])
        if not metadata(plane, account_id, library_id).get("auto_analyze"):
            continue
        root = _library_root(plane, account_id, library_id)
        failed = read_json(root / "auto-analyze.json", {})
        tried = set(failed.get("failed") or []) if isinstance(failed, dict) else set()
        for item in list_items(plane, account_id, library_id):
            item_id = str(item.get("id") or "")
            if item.get("analysis_at") or item_id in tried:
                continue
            try:
                analyze_item(plane, account_id, library_id, item_id, "127.0.0.1", model=model)
            except Exception:
                # Not retried on every tick. Turning the switch off and on clears the list.
                write_json(root / "auto-analyze.json", {"failed": sorted(tried | {item_id})})
                return ""
            return item_id
    return ""


def set_auto_analyze(plane, account_id: str, library_id: str, enabled: bool, peer_ip: str) -> bool:
    from ssios.library import _audit, _library_root, _require, metadata, replace_metadata

    _require(plane, account_id, library_id, "owner")
    data = metadata(plane, account_id, library_id)
    data["auto_analyze"] = bool(enabled)
    replace_metadata(plane, account_id, library_id, data)
    write_json(_library_root(plane, account_id, library_id) / "auto-analyze.json", {"failed": []})
    _audit(plane, account_id, peer_ip, "library.auto_analyze", library_id, "on" if enabled else "off", 0)
    return bool(enabled)


def tick(plane) -> None:
    """One pass for every active account. A failure in one account does not stop the others."""
    from ssios.calendar_intent import host_clock

    users = getattr(getattr(plane, "accounts", None), "users", None)
    if not isinstance(users, dict):
        return
    local = str(getattr(plane.settings, "llm_provider", "")) == "ollama"
    for account_id, account in list(users.items()):
        if not isinstance(account, dict) or str(account.get("status") or "active") != "active":
            continue
        row = {**account, "id": account_id}
        try:
            now, _zone = host_clock(plane, row)
            run_brief(plane, row, now)
        except Exception:
            pass
        if local:
            try:
                analyze_next(plane, row)
            except Exception:
                pass

"""What this account has read. A file or note the account has not read is found by search and is never used as memory.

Depths:
  skimmed  the account went over it quickly. A note clipped or written by the account is skimmed from the start.
           A file is skimmed once the account opens it.
  read     the account read it with care and said so, or starred it or wrote a note on it.

The ledger records the key, the depth, the signals, and the times. It holds no text.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ssios.util import read_json, write_json

DEPTHS = ("skimmed", "read")


def ledger(home) -> dict:
    data = read_json(home / "reading.json", {})
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, dict) else {}


def depth_label(depth: str) -> str:
    return {"read": "Read", "skimmed": "Skimmed"}.get(str(depth), "Not read")


def depth_of(home, key: str) -> str:
    """Empty when the account has not read it."""
    depth = str(ledger(home).get(key, {}).get("depth") or "")
    return depth if depth in DEPTHS else ""


def mark(home, key: str, depth: str, signal: str, now: datetime | None = None) -> bool:
    """Record a reading. The depth only goes up. Returns True when this is the first reading or a deeper one."""
    if depth not in DEPTHS:
        return False
    stamp = (now or datetime.now().astimezone()).replace(microsecond=0).isoformat()
    items = ledger(home)
    entry = items.get(key) if isinstance(items.get(key), dict) else {}
    before = str(entry.get("depth") or "")
    deeper = before not in DEPTHS or DEPTHS.index(depth) > DEPTHS.index(before)
    signals = [str(one) for one in entry.get("signals") or []]
    if signal and signal not in signals:
        signals.append(signal[:20])
    items[key] = {"depth": depth if deeper else before, "signals": signals[-8:], "first_at": entry.get("first_at") or stamp, "last_at": stamp}
    write_json(home / "reading.json", {"items": items})
    return deeper


def note_depth(home, rel: str) -> str:
    """A note in the vault was clipped or written by the account, so it is skimmed unless marked read."""
    return depth_of(home, "note:" + rel) or "skimmed"


def mark_note(plane, scope, rel: str, depth: str, signal: str, peer_ip: str = "127.0.0.1") -> None:
    if mark(scope.home, "note:" + rel, depth, signal) and depth == "read":
        _done_reading(scope, rel.rsplit("/", 1)[-1].removesuffix(".md"))
    plane.security_audit.write(actor=scope.account_id, ip=peer_ip or "127.0.0.1", action="reading.mark", target="note", result=depth)


def mark_file(plane, account: dict, library_id: str, item_id: str, depth: str, signal: str, peer_ip: str = "127.0.0.1") -> None:
    """Record that the account opened or worked on a Files item. A first reading lets its analysis note into memory."""
    from ssios.scope import AccountScope

    scope = AccountScope(plane, account)
    first = not depth_of(scope.home, f"file:{library_id}:{item_id}")
    changed = mark(scope.home, f"file:{library_id}:{item_id}", depth, signal)
    if not changed:
        return
    plane.security_audit.write(actor=scope.account_id, ip=peer_ip or "127.0.0.1", action="reading.mark", target=item_id, result=depth)
    from ssios.library import get_item

    item = get_item(plane, scope.account_id, library_id, item_id)
    if depth == "read":
        _done_reading(scope, str(item.get("name") or ""))
    if first and (item.get("description") or item.get("ocr")):
        from ssios.library_analyze import _write_topic

        _write_topic(plane, scope.account_id, library_id, item, peer_ip)


def has_read_file(plane, account_id: str, library_id: str, item_id: str) -> bool:
    return bool(depth_of(plane.accounts.home(account_id), f"file:{library_id}:{item_id}"))


def _done_reading(scope, title: str) -> None:
    from ssios.reminders import ReminderStore

    if title:
        ReminderStore(scope.home).complete_matching("Read carefully: " + title)


def remind_to_read(plane, scope, key: str, title: str, when: str) -> str:
    """A reminder to read something with care. Marking it read later completes the reminder."""
    from ssios.calendar_intent import host_clock
    from ssios.reminders import ReminderStore

    now, zone_name = host_clock(plane, scope.account)
    day = now.replace(minute=0, second=0, microsecond=0)
    if when == "tonight":
        due = day.replace(hour=20)
        if due <= now:
            due += timedelta(days=1)
    elif when == "weekend":
        due = (day + timedelta(days=(5 - day.weekday()) % 7 or 7)).replace(hour=10)
    elif when == "week":
        due = (day + timedelta(days=7)).replace(hour=9)
    else:
        due = (day + timedelta(days=1)).replace(hour=9)
    text = "Read carefully: " + " ".join(str(title).split())[:120]
    ReminderStore(scope.home).add(text=text, phrase=text, due=due, zone_name=zone_name, created=now)
    del key
    return due.strftime("%a %b %d, %H:%M")


def prune_unread(plane, account: dict) -> dict:
    """Take file notes out of memory for files this account has not opened. The descriptions stay in the Files catalog.

    Before this rule, analysis wrote every description into memory. This removes those for unread files.
    """
    from ssios.library import list_items, list_libraries
    from ssios.library_analyze import revoke_recall
    from ssios.scope import AccountScope

    scope = AccountScope(plane, account)
    store = scope.memory()
    kept = removed = 0
    for row in list_libraries(plane, scope.account_id):
        if row.get("offline"):
            continue
        for item in list_items(plane, scope.account_id, row["id"]):
            item_id = str(item.get("id") or "")
            try:
                current = store.read_file(scope.project_id(), f"topics/library/{item_id}.md", include_untrusted=True)
            except Exception:
                continue
            if current.get("tombstone"):
                continue
            if depth_of(scope.home, f"file:{row['id']}:{item_id}"):
                kept += 1
            else:
                revoke_recall(plane, scope.account_id, item_id, "127.0.0.1")
                removed += 1
    return {"kept_read": kept, "removed_unread": removed}

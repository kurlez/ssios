"""Per-account goals. The model may propose. Only validated explicit asks are stored.

Fetched mail and web text never reach this module. Completion requires a
successful tool or host result in the same turn, not a sentence from the model.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime

from ssios.errors import SsiosError
from ssios.util import read_json, write_json

_CHITCHAT = re.compile(
    r"^(hi|hello|hey|thanks|thank you|ok|okay|yes|no|你是谁|你好|谢谢)[\s!.?]*$",
    re.IGNORECASE,
)
_EXPLICIT = (
    re.compile(r"(?:my goal is|add a goal:?|add a task:?|todo:)\s+(.{3,160})", re.IGNORECASE),
    re.compile(r"\bremind me to\s+(.{3,160})", re.IGNORECASE),
    re.compile(
        r"\b(?:i need to|i want to|i have to)\s+(.{3,120}?)\s+(?:by|before)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|next|\d)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:记一下|记下|加个任务|加一个任务|添加一个任务|添加任务|帮我记个任务|记个任务)\s*[:：，,]?\s*(.{2,80})"),
    re.compile(r"提醒我\s*(?:去|要)?\s*(.{2,80})"),
    re.compile(r"(?:我要|我想|我得|我需要)在(.{2,40}?(?:前|之前|以前).{2,60})"),
)


def goal_mode(home) -> str:
    data = read_json(home / "goals-prefs.json", {"mode": "auto"})
    mode = str(data.get("mode") or "auto") if isinstance(data, dict) else "auto"
    return mode if mode in {"auto", "suggest", "off"} else "auto"


def set_goal_mode(home, mode: str) -> None:
    if mode not in {"auto", "suggest", "off"}:
        raise SsiosError("goal_invalid", "goal mode rejected", 400)
    write_json(home / "goals-prefs.json", {"mode": mode})


class GoalStore:
    def __init__(self, home) -> None:
        self.home = home
        self.path = home / "goals.jsonl"

    def all(self) -> list[dict]:
        if not self.path.is_file():
            return []
        latest: dict[str, dict] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            goal = row.get("goal") if isinstance(row, dict) else None
            if isinstance(goal, dict) and goal.get("id"):
                latest[str(goal["id"])] = goal
        return list(latest.values())

    def get(self, goal_id: str) -> dict | None:
        for goal in self.all():
            if goal.get("id") == goal_id:
                return goal
        return None

    def open_goals(self) -> list[dict]:
        return [goal for goal in self.all() if goal.get("status") in {"open", "in_progress"}]

    def append(self, goal: dict, previous: dict | None = None) -> dict:
        row = {"goal": goal}
        if previous is not None:
            row["previous"] = previous
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return goal

    def undo(self, goal_id: str, now: str) -> dict | None:
        previous = _previous(self.path, goal_id)
        if previous is None:
            return None
        restored = dict(previous)
        restored["updated_at"] = now
        self.append(restored, previous=self.get(goal_id))
        return restored


def _previous(path, goal_id: str) -> dict | None:
    if not path.is_file():
        return None
    found = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        goal = row.get("goal") if isinstance(row, dict) else None
        if isinstance(goal, dict) and goal.get("id") == goal_id and isinstance(row.get("previous"), dict):
            found = row["previous"]
    return found


def task_command(message: str) -> dict | None:
    """Deterministic task commands. Empty when the message is not one of them."""
    text = " ".join(str(message or "").split())
    folded = text.casefold()
    if re.search(r"\b(list my tasks|show my tasks)\b", folded) or any(cue in text for cue in ("我的任务", "列出任务", "有哪些任务")):
        return {"op": "list"}
    found = re.search(r"(?:add a task:?|帮我记个任务|记个任务)\s*[:：]?\s*(.{2,160})", text, re.IGNORECASE)
    if found:
        return {"op": "add", "title": found.group(1).strip(" .")}
    found = re.search(r"把(.+?)(?:标记完成|标为完成|设为完成)", text)
    if found:
        return {"op": "complete", "title": found.group(1).strip(" .")}
    found = re.search(r"\bmark (.+?) (?:done|complete|completed)\b", folded)
    if found:
        return {"op": "complete", "title": found.group(1).strip(" .")}
    found = re.search(r"\b(?:drop|cancel) (?:the )?task (.+)$", folded)
    if found:
        return {"op": "drop", "title": found.group(1).strip(" .")}
    found = re.search(r"\brename (?:the )?task (.+?) to (.+)$", folded)
    if found:
        return {"op": "edit", "title": found.group(1).strip(" ."), "name": found.group(2).strip(" .")}
    found = re.search(r"把任务(.+?)改成(.+)$", text)
    if found:
        return {"op": "edit", "title": found.group(1).strip(" ."), "name": found.group(2).strip(" .")}
    found = re.search(r"\bset (?:the )?task (.+?) due (.+)$", folded)
    if found:
        return {"op": "due", "title": found.group(1).strip(" ."), "when": found.group(2).strip()}
    found = re.search(r"把(.+?)改到(.+)$", text)
    if found and "任务" in text:
        return {"op": "due", "title": found.group(1).replace("任务", "").strip(" ."), "when": found.group(2).strip()}
    return None


def explicit_goal(message: str) -> dict | None:
    """A goal the user asked for. Chit-chat returns nothing."""
    text = " ".join(str(message or "").split())
    if not text or _CHITCHAT.match(text):
        return None
    if text.endswith("?") or text.endswith("？"):
        return None
    for pattern in _EXPLICIT:
        found = pattern.search(text)
        if not found:
            continue
        title = " ".join(found.group(1).split()).strip(" .，,。")
        if len(title) < 2 or _CHITCHAT.match(title):
            return None
        return {"title": title[:80], "description": text[:240]}
    return None


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\u4e00-\u9fff]{2,}", str(text or "").casefold()))


def similar(left: str, right: str) -> bool:
    first, second = _tokens(left), _tokens(right)
    if not first or not second:
        return False
    shared = len(first & second)
    return shared / min(len(first), len(second)) >= 0.6


def _validate_model(message: str, proposals: list, open_goals: list[dict], evidence: bool) -> list[dict]:
    """Keep a model proposal only when the title is in the user message."""
    user = str(message or "")
    kept = []
    for item in proposals:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:80]
        op = str(item.get("op") or "")
        if len(title) < 3 or title.casefold() not in user.casefold():
            continue
        asked = explicit_goal(user)
        if op == "create" and asked is not None and similar(title, asked["title"]) and not any(similar(title, goal.get("title") or "") for goal in open_goals):
            kept.append({"op": "create", "title": title})
        elif op == "done" and evidence and any(similar(title, goal.get("title") or "") for goal in open_goals):
            kept.append({"op": "done", "title": title})
    return kept


def model_goals(llm, message: str, open_goals: list[dict], evidence: bool) -> list[dict]:
    if llm is None or getattr(llm, "provider", "") == "mock":
        return []
    titles = ", ".join(str(goal.get("title") or "") for goal in open_goals[:12])
    messages = [
        {
            "role": "system",
            "content": (
                "Return JSON only: {\"goals\":[{\"op\":\"create\"|\"done\",\"title\":\"...\"}]}. "
                "temperature 0. Create only an explicit ask that appears in the user message. "
                "Do not use any other text. done is allowed only when evidence is true. "
                f"evidence={str(evidence).lower()}. Open goals: {titles or 'none'}."
            ),
        },
        {"role": "user", "content": str(message or "")[:1000]},
    ]
    try:
        import threading
        import time

        raw = llm.complete_chat(messages, cancel_event=threading.Event(), deadline=time.monotonic() + 8)
        data = json.loads(str(raw))
    except (SsiosError, json.JSONDecodeError, TypeError, ValueError, OSError):
        return []
    goals = data.get("goals") if isinstance(data, dict) else None
    if not isinstance(goals, list):
        return []
    return _validate_model(message, goals, open_goals, evidence)


def plan_changes(message: str, open_goals: list[dict], *, evidence: bool, model_items: list[dict] | None = None) -> list[dict]:
    """What to create or complete. Tool text is not an argument."""
    changes = []
    created = explicit_goal(message)
    if created and not any(similar(created["title"], goal.get("title") or "") for goal in open_goals):
        changes.append({"op": "create", **created})
    elif created:
        changes.append({"op": "duplicate", "title": created["title"]})
    if evidence:
        for goal in open_goals:
            title = str(goal.get("title") or "")
            if similar(title, message):
                changes.append({"op": "done", "id": goal["id"], "title": title})
                break
    for item in model_items or []:
        title = str(item.get("title") or "")
        if item.get("op") == "create":
            asked = explicit_goal(message)
            if asked is None or not similar(title, asked["title"]):
                continue
            if any(similar(title, goal.get("title") or "") for goal in open_goals):
                continue
            if any(similar(title, change.get("title") or "") for change in changes):
                continue
            changes.append({"op": "create", "title": title, "description": message[:240]})
        if item.get("op") == "done" and evidence:
            for goal in open_goals:
                if similar(title, goal.get("title") or "") and not any(change.get("id") == goal.get("id") for change in changes):
                    changes.append({"op": "done", "id": goal["id"], "title": goal.get("title") or title})
    return changes


def apply_turn(scope, conversation_id: str, message_id: str, message: str, *, evidence: bool, evidence_reason: str, now: datetime, peer_ip: str) -> list[dict]:
    """Apply or suggest. Returns chips. Audit lines have ids only."""
    home = scope.home
    mode = goal_mode(home)
    if mode == "off":
        return []
    store = GoalStore(home)
    open_goals = store.open_goals()
    model_items = model_goals(getattr(scope.plane, "llm", None), message, open_goals, evidence)
    changes = plan_changes(message, open_goals, evidence=evidence, model_items=model_items)
    chips = []
    stamp = now.replace(microsecond=0).isoformat()
    for change in changes:
        if change["op"] == "duplicate":
            continue
        if change["op"] == "create":
            goal = {
                "id": "goal-" + secrets.token_hex(4),
                "title": change["title"],
                "description": change.get("description") or "",
                "source": {"conversation_id": conversation_id, "message_id": message_id},
                "created_at": stamp,
                "updated_at": stamp,
                "status": "open",
                "due": _due(scope, message, now),
                "evidence": None,
            }
            if mode == "suggest":
                chips.append({"kind": "suggested", "id": "", "title": goal["title"], "goal": goal})
                continue
            store.append(goal)
            _audit(scope, peer_ip, "goal.create", goal["id"], "open")
            chips.append({"kind": "added", "id": goal["id"], "title": goal["title"]})
        elif change["op"] == "done" and mode == "auto":
            current = store.get(str(change["id"]))
            if not current or current.get("status") not in {"open", "in_progress"}:
                continue
            updated = dict(current)
            updated["status"] = "done"
            updated["updated_at"] = stamp
            updated["evidence"] = {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "reason": evidence_reason[:80] or "tool result",
            }
            store.append(updated, previous=current)
            _audit(scope, peer_ip, "goal.complete", updated["id"], "done")
            from ssios.reminders import ReminderStore

            ReminderStore(scope.home).cancel_for_task(updated["id"])
            chips.append({"kind": "completed", "id": updated["id"], "title": updated["title"]})
    if any(chip["kind"] in {"added", "completed"} for chip in chips):
        mirror_vault(scope, store.all())
    return chips


def listed_goals(goals: list[dict]) -> list[dict]:
    """Open and done goals from a store query. Dropped goals are omitted."""
    shown = []
    for goal in goals:
        if not isinstance(goal, dict):
            continue
        if goal.get("status") not in {"open", "in_progress", "done"}:
            continue
        shown.append(goal)
    return shown


def task_view(goal: dict, *, writable: bool = True, hint: str = "") -> dict:
    """A checklist row. The title and status come from the stored goal."""
    view = {
        "id": str(goal.get("id") or ""),
        "title": str(goal.get("title") or ""),
        "status": "done" if goal.get("status") == "done" else "open",
        "due": str(goal.get("due") or ""),
        "writable": bool(writable and goal.get("id")),
    }
    if hint:
        view["hint"] = hint
    return view


def undo_goal(scope, goal_id: str, now: datetime, peer_ip: str) -> dict | None:
    stamp = now.replace(microsecond=0).isoformat()
    restored = GoalStore(scope.home).undo(goal_id, stamp)
    if restored is None:
        return None
    _audit(scope, peer_ip, "goal.undo", goal_id, str(restored.get("status") or "open"))
    mirror_vault(scope, GoalStore(scope.home).all())
    return restored


def manual_goal(scope, *, title: str, description: str, status: str, goal_id: str, now: datetime, peer_ip: str, conversation_id: str = "") -> dict:
    if status not in {"open", "in_progress", "done", "dropped"}:
        raise SsiosError("goal_invalid", "goal status rejected", 400)
    title = " ".join(str(title or "").split())[:80]
    if len(title) < 3:
        raise SsiosError("goal_invalid", "goal title rejected", 400)
    store = GoalStore(scope.home)
    stamp = now.replace(microsecond=0).isoformat()
    current = store.get(goal_id) if goal_id else None
    if current is None:
        goal = {
            "id": "goal-" + secrets.token_hex(4),
            "title": title,
            "description": description[:240],
            "source": {"conversation_id": conversation_id, "message_id": ""},
            "created_at": stamp,
            "updated_at": stamp,
            "status": status,
            "due": None,
            "evidence": {"conversation_id": conversation_id, "message_id": "", "reason": "manual"} if status == "done" else None,
        }
        store.append(goal)
        _audit(scope, peer_ip, "goal.create", goal["id"], status)
    else:
        goal = dict(current)
        goal["title"] = title
        goal["description"] = description[:240]
        goal["status"] = status
        goal["updated_at"] = stamp
        if status == "done" and not goal.get("evidence"):
            goal["evidence"] = {"conversation_id": conversation_id, "message_id": "", "reason": "manual"}
        store.append(goal, previous=current)
        _audit(scope, peer_ip, "goal.update", goal["id"], status)
        if status in {"done", "dropped"}:
            from ssios.reminders import ReminderStore

            ReminderStore(scope.home).cancel_for_task(goal["id"])
    mirror_vault(scope, store.all())
    return goal


def mirror_vault(scope, goals: list[dict]) -> None:
    from ssios.vault import account_vault, vault_base

    try:
        root = account_vault(scope.plane.settings, scope.account_id)
        base = vault_base(scope.plane.settings)
    except SsiosError:
        return
    if not root.is_dir() and not base.is_dir():
        return
    root.mkdir(parents=True, exist_ok=True)
    lines = ["# Tasks", ""]
    for goal in goals:
        if goal.get("status") == "dropped":
            continue
        mark = "x" if goal.get("status") == "done" else " "
        due = f" (due {goal['due']})" if goal.get("due") else ""
        lines.append(f"- [{mark}] {goal.get('title') or ''}{due}")
    if len(lines) == 2:
        lines.append("- [ ] No open goals.")
    try:
        from ssios.reminders import ReminderStore

        for item in ReminderStore(scope.home).open_items():
            repeat = f" ({item['repeat']})" if item.get("repeat") else ""
            lines.append(f"- [ ] {item.get('due')} {item.get('text')}{repeat}")
    except (SsiosError, OSError, TypeError, ValueError):
        pass
    from ssios.vault import own_vault

    path = root / "Tasks.md"
    try:
        external = own_vault(scope.plane.settings, scope.account_id) is not None
    except SsiosError:
        return
    if external:
        # The vault is the account's own Obsidian vault. Its Tasks.md is not SSIOS's to overwrite.
        (root / "SSIOS").mkdir(exist_ok=True)
        path = root / "SSIOS" / "Tasks.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _due(scope, message: str, now: datetime):
    from ssios.calendar_intent import account_zone
    from ssios.reminders import capture_due

    zone, _name = account_zone(scope.plane.settings, scope.account)
    due = capture_due(message, now, zone)
    if due is None:
        return None
    return due.replace(microsecond=0).isoformat()


def _audit(scope, peer_ip: str, action: str, goal_id: str, result: str) -> None:
    scope.plane.security_audit.write(
        actor=scope.account_id,
        ip=peer_ip or "127.0.0.1",
        action=action,
        target=goal_id,
        result=result,
    )


def turn_evidence(record: dict) -> tuple[bool, str]:
    """True when this turn has a tool or host result. Model prose is not evidence."""
    messages = record.get("messages") or []
    last_user = -1
    for index, item in enumerate(messages):
        if item.get("role") == "user":
            last_user = index
    if last_user < 0:
        return False, ""
    for item in messages[last_user + 1 :]:
        if item.get("error_code"):
            continue
        if item.get("results"):
            return True, "tool result"
        route = item.get("route") if isinstance(item.get("route"), dict) else {}
        if route.get("route") in {"schedule", "account"} and item.get("text"):
            return True, "host answer"
    return False, ""

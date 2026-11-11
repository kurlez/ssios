"""Host MCP tools for the no-NIC guest.

The guest sends a tool name and arguments over the existing vsock proxy.
This module runs the tool on the host. OAuth tokens, site cookies, and
passwords never enter the result. A write returns a pending approval and
does not call Google or git until that approval is consumed.
"""

from __future__ import annotations

import json
import re

from ssios.errors import SsiosError
from ssios.google_connector import UNTRUSTED_PREFIX

READ_TOOLS = {
    "gmail.search": "gmail.search",
    "gmail.read": "gmail.get_message",
    "drive.list": "drive.search",
    "drive.read": "drive.read_file",
    "calendar.list": "calendar.list_events",
    "memory.read": "",
    "memory.search": "",
    "tasks.list": "",
    "reminders.list": "",
    "web.fetch": "",
    "weather": "",
}
WRITE_TOOLS = {
    "calendar.create": "calendar.create_event",
    "calendar.update": "calendar.update_event",
    "memory.propose_write": "",
    "tasks.add": "",
    "tasks.complete": "",
    "tasks.drop": "",
    "reminders.add": "",
    "reminders.cancel": "",
    "web.clip": "",
}
TOOL_NAMES = tuple(READ_TOOLS) + tuple(WRITE_TOOLS)
_PREFIX = {
    "gmail.search": "m",
    "gmail.read": "m",
    "drive.list": "d",
    "drive.read": "d",
    "calendar.list": "c",
    "calendar.create": "c",
    "calendar.update": "c",
    "memory.read": "n",
    "memory.search": "n",
    "memory.propose_write": "n",
    "tasks.list": "t",
    "tasks.add": "t",
    "tasks.complete": "t",
    "tasks.drop": "t",
    "reminders.list": "r",
    "reminders.add": "r",
    "reminders.cancel": "r",
    "web.fetch": "w",
    "web.clip": "w",
    "weather": "w",
}
_SECRET = re.compile(r"ya29\.|refresh_token|client_secret|access_token", re.IGNORECASE)
_MIRROR_HOSTS = ("archive.is", "archive.org", "webcache.googleusercontent.com", "outline.com", "12ft.io")


class ToolSession:
    """One turn's tool calls. Result ids stay stable for citations."""

    def __init__(self, plane, account: dict, peer_ip: str = "127.0.0.1") -> None:
        self.plane = plane
        self.account = account
        self.peer_ip = peer_ip or "127.0.0.1"
        self.results: list[dict] = []

    def call(self, name: str, arguments: dict | None = None) -> dict:
        result = call_tool(self.plane, self.account, name, arguments or {}, self.peer_ip, len(self.results) + 1)
        self.results.append(result)
        return result


def call_tool(plane, account: dict, name: str, arguments: dict, peer_ip: str = "127.0.0.1", index: int = 1) -> dict:
    tool = str(name or "")
    if tool not in TOOL_NAMES:
        raise SsiosError("mcp_tool_denied", "That tool is not available to the guest.", 403)
    args = _arguments(arguments)
    _bound_account(account, args)
    args.pop("account_id", None)
    result_id = f"{_PREFIX.get(tool, 'x')}{int(index)}"
    if tool in WRITE_TOOLS:
        return _write(plane, account, tool, args, peer_ip, result_id)
    return _read(plane, account, tool, args, peer_ip, result_id)


def apply_write(scope, record: dict) -> dict:
    """Run a pending MCP write. Called only after the owner approves it."""
    raw = scope.staged_bytes(str(record.get("proposal_id") or ""))
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SsiosError("payload_mismatch", "MCP write payload is unreadable", 409) from exc
    if not isinstance(payload, dict):
        raise SsiosError("payload_mismatch", "MCP write payload is unreadable", 409)
    tool = str(payload.get("tool") or "")
    args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    if tool == "tasks.add":
        return _apply_task(scope, args)
    if tool == "tasks.complete":
        return _apply_task_status(scope, args, "done")
    if tool == "tasks.drop":
        return _apply_task_status(scope, args, "dropped")
    if tool == "reminders.add":
        return _apply_reminder(scope, args)
    if tool == "reminders.cancel":
        return _apply_reminder_cancel(scope, args)
    if tool == "memory.propose_write":
        return {"action": "memory.commit", "status": "already-proposed"}
    if tool in {"calendar.create", "calendar.update"}:
        return {"action": "connector.write", "status": "pending-on-connector"}
    if tool == "web.clip":
        return {"action": "vault.clip", "status": "staged"}
    raise SsiosError("mcp_tool_denied", "That write cannot be applied.", 403)


def _arguments(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SsiosError("mcp_tool_denied", "Tool arguments must be an object.", 400)
    blob = json.dumps(value, ensure_ascii=False)
    if _SECRET.search(blob):
        raise SsiosError("mcp_tool_denied", "Tool arguments must not carry a token.", 403)
    return dict(value)


def _bound_account(account: dict, arguments: dict) -> None:
    named = str(arguments.get("account_id") or "")
    owner = str(account.get("id") or "")
    if named and named != owner:
        raise SsiosError("mcp_tool_denied", "A tool cannot name another account.", 403)


def _read(plane, account: dict, tool: str, arguments: dict, peer_ip: str, result_id: str) -> dict:
    from ssios.scope import AccountScope

    scope = AccountScope(plane, account)
    try:
        if tool in {"gmail.search", "gmail.read", "drive.list", "drive.read", "calendar.list"}:
            body, count = _connector_read(plane, account, READ_TOOLS[tool], arguments)
        elif tool == "memory.read":
            body, count = _memory_read(scope, arguments)
        elif tool == "memory.search":
            body, count = _memory_search(scope, arguments)
        elif tool == "tasks.list":
            body, count = _tasks_list(scope)
        elif tool == "reminders.list":
            body, count = _reminders_list(scope)
        elif tool == "web.fetch":
            body, count = _web_fetch(plane, scope, arguments, peer_ip)
        else:
            body, count = _weather(plane, scope, arguments, peer_ip)
    except SsiosError as exc:
        _audit(plane, account["id"], peer_ip, tool, "error", 0, 0)
        raise exc
    text = _fence(result_id, body)
    _audit(plane, account["id"], peer_ip, tool, "ok", count, len(text.encode("utf-8")))
    return {"status": "ok", "id": result_id, "kind": "read", "tool": tool, "text": text, "item_count": count}


def _write(plane, account: dict, tool: str, arguments: dict, peer_ip: str, result_id: str) -> dict:
    from ssios.scope import AccountScope

    scope = AccountScope(plane, account)
    if tool in {"calendar.create", "calendar.update"}:
        approval_id = _propose_calendar(plane, account, WRITE_TOOLS[tool], arguments)
    elif tool == "memory.propose_write":
        approval_id = _propose_memory(scope, arguments)
    elif tool == "web.clip":
        approval_id = _propose_clip(plane, scope, arguments, peer_ip)
    else:
        approval_id = _propose_local(scope, tool, arguments)
    _audit(plane, account["id"], peer_ip, tool, "pending", 0, 0)
    return {
        "status": "pending",
        "id": result_id,
        "kind": "write",
        "tool": tool,
        "approval_id": approval_id,
        "text": f"Pending approval {approval_id}. Nothing was written.",
    }


def _connector_read(plane, account: dict, tool: str, arguments: dict) -> tuple[str, int]:
    from ssios.google_connector import session_for_account

    result = plane.google.read_now(
        session_for_account(account),
        project_id="notes",
        tool=tool,
        arguments=_public_args(arguments),
        limits={"max_items": int(arguments.get("limit") or 20), "max_bytes": 48000, "max_calls": 40},
        peer_ip="127.0.0.1",
    )
    text = str(result.get("text") or "")
    return text, int(result.get("item_count") or 0)


def _propose_calendar(plane, account: dict, tool: str, arguments: dict) -> str:
    from ssios.google_connector import session_for_account

    proposed = plane.google.propose_fetch(
        session_for_account(account),
        project_id="notes",
        tool=tool,
        arguments=_public_args(arguments),
        limits={"max_items": 5, "max_bytes": 4096, "max_calls": 4},
    )
    approval = proposed.get("approval") if isinstance(proposed, dict) else None
    if not isinstance(approval, dict) or not approval.get("id"):
        raise SsiosError("mcp_tool_denied", "The write did not create an approval.", 403)
    return str(approval["id"])


def _public_args(arguments: dict) -> dict:
    blocked = {"account_id", "token", "access_token", "refresh_token", "cookie", "authorization"}
    return {key: value for key, value in arguments.items() if key not in blocked}


def _memory_read(scope, arguments: dict) -> tuple[str, int]:
    path = str(arguments.get("path") or "human.md")
    if path.startswith("/") or ".." in path or not path.endswith(".md"):
        raise SsiosError("path_rejected", "memory path rejected", 400)
    doc = scope.memory().read_file(scope.project_id(), path, include_untrusted=False)
    content = str(doc.get("content") or "")
    return content, 1 if content else 0


def _memory_search(scope, arguments: dict) -> tuple[str, int]:
    query = str(arguments.get("q") or arguments.get("query") or "")[:80]
    found = scope.memory().search(scope.project_id(), query, include_untrusted=False)
    hits = found.get("hits") if isinstance(found, dict) else []
    lines = []
    for hit in hits or []:
        if isinstance(hit, dict):
            lines.append(str(hit.get("path") or ""))
    return "\n".join(lines), len(lines)


def _propose_memory(scope, arguments: dict) -> str:
    path = str(arguments.get("path") or "human.md")
    content = str(arguments.get("content") or "")
    if path.startswith("/") or ".." in path or not path.endswith(".md"):
        raise SsiosError("path_rejected", "memory path rejected", 400)
    if not content.strip():
        raise SsiosError("mcp_tool_denied", "A memory proposal needs content.", 400)
    store = scope.memory()
    proposal = store.propose(
        scope.project_id(),
        store.head(scope.project_id()),
        [{"path": path, "content": content}],
        task_id="mcp",
    )
    approval = scope.create_memory_approval(proposal)
    return str(approval["id"])


def _tasks_list(scope) -> tuple[str, int]:
    from ssios.goals import GoalStore

    rows = GoalStore(scope.home).open_goals()
    lines = [f"{item.get('id')} {item.get('title')}" for item in rows]
    return "\n".join(lines), len(lines)


def _reminders_list(scope) -> tuple[str, int]:
    from ssios.reminders import ReminderStore

    rows = ReminderStore(scope.home).open_items()
    lines = [f"{item.get('id')} {item.get('due')} {item.get('text')}" for item in rows]
    return "\n".join(lines), len(lines)


def _propose_local(scope, tool: str, arguments: dict) -> str:
    payload = json.dumps({"tool": tool, "arguments": _public_args(arguments)}, ensure_ascii=False).encode("utf-8")
    approval = scope.create_approval(
        action="mcp.write",
        project_id=scope.project_id(),
        task_id="mcp",
        payload=payload,
        meta={"expected_revision": "mcp", "tool": tool},
    )
    return str(approval["id"])


def _web_fetch(plane, scope, arguments: dict, peer_ip: str) -> tuple[str, int]:
    url = str(arguments.get("url") or "")
    host = url.split("/")[2] if "://" in url else ""
    if any(host == item or host.endswith("." + item) for item in _MIRROR_HOSTS):
        raise SsiosError("skill_denied", "Archive mirrors are not used.", 403)
    fetched = plane.skills.fetch(url, plane.skills.policy(), scope.account_id, "web_fetch", peer_ip)
    from ssios.skill_broker import _article

    title, body = _article(fetched["body"])
    return f"{title}\n{body}", 1 if body else 0


def _weather(plane, scope, arguments: dict, peer_ip: str) -> tuple[str, int]:
    place = str(arguments.get("place") or arguments.get("location") or "")
    result = plane.skills.run(scope.account_id, "weather", peer_ip=peer_ip, skill="weather", place=place)
    if result.get("need"):
        return str(result["need"].get("prompt") or "Which city?"), 0
    summary = ""
    blocks = result.get("blocks") or []
    if blocks:
        summary = str(blocks[0].get("summary") or "")
    return summary or str(result.get("text") or ""), 1 if summary else 0


def _propose_clip(plane, scope, arguments: dict, peer_ip: str) -> str:
    url = str(arguments.get("url") or "")
    fetched = _web_fetch(plane, scope, {"url": url}, peer_ip)
    payload = fetched[0].encode("utf-8")
    approval = scope.create_approval(
        action="vault.clip",
        project_id=scope.project_id(),
        task_id="mcp",
        payload=payload,
        meta={"expected_revision": "mcp", "path": "clip.md", "title": "Clip", "staging": ""},
    )
    return str(approval["id"])


def _apply_task(scope, arguments: dict) -> dict:
    import secrets
    from datetime import datetime, timezone

    from ssios.goals import GoalStore

    title = " ".join(str(arguments.get("title") or "").split())[:160]
    if len(title) < 2:
        raise SsiosError("mcp_tool_denied", "A task needs a title.", 400)
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    goal = {
        "id": "goal-" + secrets.token_hex(4),
        "title": title,
        "description": "",
        "source": {"conversation_id": "", "message_id": ""},
        "created_at": stamp,
        "updated_at": stamp,
        "status": "open",
        "due": "",
        "evidence": None,
    }
    GoalStore(scope.home).append(goal)
    return {"action": "mcp.write", "goal_id": goal["id"]}


def _apply_task_status(scope, arguments: dict, status: str) -> dict:
    from ssios.goals import GoalStore

    store = GoalStore(scope.home)
    title = str(arguments.get("title") or "")
    goal_id = str(arguments.get("id") or "")
    current = store.get(goal_id) if goal_id else None
    if current is None:
        for item in store.open_goals():
            if title and title.casefold() in str(item.get("title") or "").casefold():
                current = item
                break
    if current is None:
        raise SsiosError("not_found", "task not found", 404)
    updated = dict(current)
    updated["status"] = status
    store.append(updated, previous=current)
    return {"action": "mcp.write", "goal_id": updated["id"], "status": status}


def _apply_reminder(scope, arguments: dict) -> dict:
    from datetime import datetime

    from ssios.reminders import ReminderStore

    text = " ".join(str(arguments.get("text") or "").split())[:160]
    due_raw = str(arguments.get("due") or "")
    if not text or not due_raw:
        raise SsiosError("mcp_tool_denied", "A reminder needs text and an absolute due time.", 400)
    due = datetime.fromisoformat(due_raw)
    item = ReminderStore(scope.home).add(text=text, phrase=text, due=due, zone_name="UTC", created=due)
    return {"action": "mcp.write", "reminder_id": item["id"]}


def _apply_reminder_cancel(scope, arguments: dict) -> dict:
    from ssios.reminders import ReminderStore

    needle = str(arguments.get("text") or "")
    done = ReminderStore(scope.home).complete_matching(needle)
    return {"action": "mcp.write", "cancelled": len(done)}


def _fence(result_id: str, body: str) -> str:
    from ssios.vmguest import redact_text

    cleaned = redact_text(str(body or ""), [])
    return f"{UNTRUSTED_PREFIX}result_id={result_id}\n{cleaned}"


def _audit(plane, account_id: str, peer_ip: str, tool: str, result: str, count: int, size: int) -> None:
    service = tool.split(".", 1)[0]
    try:
        plane.security_audit.write(
            actor=account_id,
            ip=peer_ip or "127.0.0.1",
            action="mcp.read" if result == "ok" else "mcp.write",
            target=tool,
            result=result,
            extra={"service": service, "item_count": str(count), "byte_count": str(size), "reason_code": result},
        )
    except Exception:
        return

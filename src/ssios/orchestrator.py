"""Route a chat turn to Codex, or keep an explicit command on the host.

Codex runs in the guest. When the guest is not available, the host uses the
existing deterministic path and says why.
"""

from __future__ import annotations

import re
from pathlib import Path

from ssios.chat_router import RouteDecision, classify

_REPORT = re.compile(r"总结|概括|汇报|\b(summarize|summarise|summary|report|digest)\b", re.IGNORECASE)
_UNDO = re.compile(r"^(undo|撤销)(\b| )", re.IGNORECASE)
_FIELD_FORM = re.compile(r"\b(create|update|delete)\s+calendar\s+event\b", re.IGNORECASE)
_FALLBACK = "Codex guest is not available, so this stayed on the host."


def repo_agents() -> str:
    root = Path(__file__).resolve().parents[2] / "agents" / "codex"
    parts = []
    guide = root / "AGENTS.md"
    if guide.is_file():
        parts.append(guide.read_text(encoding="utf-8").strip())
    skills = root / "skills"
    if skills.is_dir():
        for path in sorted(skills.glob("*.md")):
            parts.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts)


def choose_route(message: str, *, guest_ready: bool, override: str = "auto", llm=None, pending_fill: bool = False) -> tuple[RouteDecision, str]:
    """Return the route and a fallback note. The note is empty when Codex ran or was not needed."""
    from ssios.calendar_intent import no_tool_request

    if no_tool_request(message) and override != "force_vm":
        return classify(message, override, llm=llm), ""
    if _explicit_host(message) and override != "force_vm":
        return classify(message, override, llm=llm), ""
    decision = classify(message, override, llm=llm)
    if override == "force_vm" and guest_ready:
        return (
            RouteDecision(
                route="codex",
                reason="You chose the guest for this message.",
                reason_code="override_vm",
                source="override",
                services=decision.services,
                skill=decision.skill,
            ),
            "",
        )
    if not _needs_codex(message, decision, pending_fill):
        return decision, ""
    if guest_ready and override != "force_direct":
        return (
            RouteDecision(
                route="codex",
                reason="This turn needs tools or more than one step, so Codex runs in the guest.",
                reason_code="rule_codex",
                source="rule",
                services=decision.services,
                skill=decision.skill,
            ),
            "",
        )
    if override == "force_direct":
        return decision, ""
    return decision, _FALLBACK


def guest_ready(plane) -> bool:
    """True only when a launcher is installed and its prerequisites are present.

    A missing launcher is not ready, even if the kernel and the Codex binary are on disk.
    A test double can set orchestrator_ready. The production guest uses the real report.
    """
    guest = getattr(plane, "chat_guest", None)
    if guest is None or not callable(getattr(guest, "run", None)):
        return False
    forced = getattr(guest, "orchestrator_ready", None)
    if isinstance(forced, bool):
        return forced
    try:
        from ssios.chat_guest import chat_guest_report

        return bool(chat_guest_report(plane.settings).get("ok"))
    except Exception:
        return False


def _needs_codex(message: str, decision: RouteDecision, pending_fill: bool) -> bool:
    """Reports, several sources, workspace work, and a pending follow-up. Writing stays on local chat."""
    from ssios.calendar_intent import no_tool_request

    if no_tool_request(message) or decision.route in {"skill", "memory", "reminders", "account", "schedule", "files"}:
        return False
    if decision.route == "direct" and not (_REPORT.search(message) or pending_fill):
        return False
    if decision.route == "skill":
        return False
    if pending_fill or _REPORT.search(message):
        return True
    if _FIELD_FORM.search(message):
        return False
    if decision.route in {"vm", "connector_vm"}:
        return True
    if len(decision.services) > 1:
        return True
    text = str(message or "")
    if re.search(r"\band\b.+\b(task|reminder)\b", text, re.IGNORECASE) or ("然后" in text and ("任务" in text or "提醒" in text)):
        return True
    return False


def present_answer(text: str, results: list[dict]) -> str:
    """Keep the model's words. Drop a citation that does not match a tool result, then append Sources."""
    reads = [item for item in results if item.get("kind") == "read"]
    if reads and all(int(item.get("item_count") or 0) == 0 for item in reads):
        text = "No items were returned, so this is the tool result.\n" + "\n".join(str(item.get("text") or "") for item in reads)
    allowed = set()
    for index, item in enumerate(results, start=1):
        allowed.add(str(item.get("id") or ""))
        allowed.add(str(index))
    cleaned = re.sub(r"\[([A-Za-z]?\d+)\]", lambda match: match.group(0) if match.group(1) in allowed else "", str(text or ""))
    lines = [cleaned.strip(), "", "Sources"]
    for item in results:
        label = str(item.get("tool") or item.get("id") or "")
        lines.append(f"[{item.get('id')}] {label}".strip())
    if not results:
        from ssios.mcp_host import text_only_tool_calls

        if text_only_tool_calls(cleaned):
            return ""
        return (cleaned.strip() + "\n") if cleaned.strip() else ""
    return "\n".join(lines).strip() + "\n"


def codex_prompt(message: str, *, pending_value: str = "") -> str:
    extra = ""
    if pending_value:
        extra = f"\n\nThe previous question was waiting for a value. The user answered: {pending_value}\n"
    note = _direct_answer_note(message)
    if note:
        extra += "\n" + note
    return repo_agents() + extra + "\n\nUSER\n" + message


def _direct_answer_note(message: str) -> str:
    """A no-tool request is answered in the final message, not with a clarifying question."""
    from ssios.calendar_intent import creative_request

    text = " ".join(str(message or "").split())
    if not creative_request(text):
        return ""
    if re.search(r"翻译|改写|润色|\b(translate|rewrite|rephrase)\b", text, re.IGNORECASE):
        return "This request needs no tool. Do the translation or rewrite in the final message. Do not ask a question first."
    return (
        "This request needs no tool. Write it in the final message. "
        "Do not ask for a theme, characters, or plot first. "
        "A short story is about 300 to 600 Chinese characters when the user writes Chinese, "
        "or about 200 to 400 English words. After the piece, you may offer one optional change."
    )


def _explicit_host(message: str) -> bool:
    from ssios.account_memory import name_question
    from ssios.goals import task_command
    from ssios.reminders import reminder_command, wants_forget, wants_remember

    text = " ".join(str(message or "").split())
    folded = text.casefold()
    reads = bool(re.search(r"\b(read|messages|inbox|mail)\b", folded) or any(word in text for word in ("邮件", "邮箱", "收件箱")))
    writes = bool(re.search(r"\b(task|reminder)\b", folded) or any(word in text for word in ("任务", "提醒")))
    if reads and writes:
        return False
    if task_command(text) or reminder_command(text) or wants_forget(text) or wants_remember(text):
        return True
    if name_question(text) or _UNDO.match(text):
        return True
    return False

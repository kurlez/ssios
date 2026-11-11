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
    if _explicit_host(message) and override != "force_vm":
        return classify(message, override, llm=llm), ""
    decision = classify(message, override, llm=llm)
    report = bool(_REPORT.search(message))
    if not pending_fill and not report and decision.route == "direct" and decision.reason_code == "rule_direct":
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
    guest = getattr(plane, "chat_guest", None)
    forced = getattr(guest, "orchestrator_ready", None)
    if isinstance(forced, bool):
        return forced
    try:
        from ssios.chat_guest import chat_guest_report

        return bool(chat_guest_report(plane.settings).get("ok"))
    except Exception:
        return False


def present_answer(text: str, results: list[dict]) -> str:
    """Keep the model's words. Drop a citation that does not match a tool result, then append Sources."""
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
        lines.append("(none)")
    return "\n".join(lines).strip() + "\n"


def codex_prompt(message: str, *, pending_value: str = "") -> str:
    extra = ""
    if pending_value:
        extra = f"\n\nThe previous question was waiting for a value. The user answered: {pending_value}\n"
    return repo_agents() + extra + "\n\nUSER\n" + message


def _explicit_host(message: str) -> bool:
    from ssios.account_memory import name_question
    from ssios.goals import task_command
    from ssios.reminders import reminder_command, wants_forget, wants_remember

    text = " ".join(str(message or "").split())
    if task_command(text) or reminder_command(text) or wants_forget(text) or wants_remember(text):
        return True
    if name_question(text) or _UNDO.match(text):
        return True
    return False

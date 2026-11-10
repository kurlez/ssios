"""Transparent route classifier.

Rules decide first. A local model may break a tie only when the rules are
unsure, and only when the provider is local Ollama. Anything unsure about
execution or writes stays on the microVM. Overrides are explicit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_QUESTION = re.compile(
    r"^(what|why|how|when|who|where|explain|describe|hello|hi|thanks|thank you)\b",
    re.IGNORECASE,
)
_EXECUTE = re.compile(
    r"\b(run|execute|compile|pytest|shell|bash|terminal|codex|script|python3?)\b",
    re.IGNORECASE,
)
_WRITE = re.compile(
    r"\b(edit|modify|patch|implement)\b|\bfix (the |a )?bug\b|\b(create|write|save|delete)\b.{0,32}\b(file|files|code|readme|workspace|script)\b|\bworkspace\b",
    re.IGNORECASE,
)
_WRITE_FILE = re.compile(
    r"\b(edit|create|write|modify|patch|delete|save)\b.{0,24}\b[\w.-]+\.(py|md|txt|json|toml)\b",
    re.IGNORECASE,
)
_GOOGLE = re.compile(
    r"\b(gmail|inbox|e-mail|email|unread|google drive|my drive|drive file)\b",
    re.IGNORECASE,
)
_DEFINITION = re.compile(r"^(what|who|explain|describe)\b", re.IGNORECASE)
_REMEMBER = re.compile(r"^(remember\b|save (this|that) to memory\b)", re.IGNORECASE)


@dataclass(frozen=True)
class RouteDecision:
    route: str
    reason: str
    reason_code: str
    source: str
    services: tuple[str, ...] = ()

    def public(self) -> dict:
        return {
            "route": self.route,
            "reason": self.reason,
            "source": self.source,
            "services": list(self.services),
        }


def classify(text: str, override: str = "auto", llm=None, deadline: float | None = None) -> RouteDecision:
    message = " ".join(str(text or "").split())
    services = _services(message)
    forced = override if override in {"force_direct", "force_vm", "auto"} else "auto"
    if forced == "force_vm":
        if services:
            return _decision("connector_vm", "override_vm", "You chose the guest. Cloud data is read only after you approve it.", services)
        return _decision("vm", "override_vm", "You chose the guest for this message.", ())
    if forced == "force_direct":
        if services:
            return _decision("connector", "override_direct", "You chose a direct answer. Cloud data is still read only after you approve it.", services)
        if _REMEMBER.search(message):
            return _decision("memory", "override_direct", "You chose not to use the guest. A memory write still waits for your approval.", ())
        return _decision("direct", "override_direct", "You chose a direct answer. No tools will run.", ())
    if services and (_writes(message) or _EXECUTE.search(message)):
        return _decision(
            "connector_vm",
            "rule_connector_vm",
            "The message needs cloud data and a workspace change. The read is approved first. The guest starts only after that.",
            services,
        )
    if services and not (_DEFINITION.search(message) and "my" not in message.casefold() and "inbox" not in message.casefold()):
        return _decision("connector", "rule_connector", "The message asks for Gmail or Drive. Nothing is fetched until you approve it.", services)
    if _writes(message) or _EXECUTE.search(message):
        return _decision("vm", "rule_vm", "The message asks to run code or change a workspace, so it goes to the guest.", ())
    if _REMEMBER.search(message):
        return _decision("memory", "rule_memory", "The message asks to save memory. It is not written until you approve it.", ())
    if _QUESTION.search(message) or message.endswith("?"):
        return _decision("direct", "rule_direct", "The message is a question, so the local model answers directly.", ())
    return _unsure(message, llm, deadline)


def _writes(message: str) -> bool:
    return bool(_WRITE_FILE.search(message) or _WRITE.search(message))


def _services(message: str) -> tuple[str, ...]:
    folded = message.casefold()
    if _DEFINITION.search(message) and "my" not in folded and "inbox" not in folded and "unread" not in folded:
        return ()
    found = []
    if any(word in folded for word in ("gmail", "inbox", "e-mail", "email", "unread")):
        found.append("gmail")
    if any(word in folded for word in ("google drive", "my drive", "drive file")) or re.search(r"\bdrive\b", folded):
        found.append("drive")
    return tuple(found)


def _unsure(message: str, llm, deadline: float | None) -> RouteDecision:
    provider = getattr(llm, "provider", "")
    judge = getattr(llm, "judge_route", None)
    if provider == "ollama" and callable(judge):
        try:
            verdict = str(judge(message, deadline=deadline))
        except Exception:
            verdict = "vm"
        if verdict.strip().lower() == "direct":
            return _decision("direct", "model_direct", "The local model judged this as a question.", ())
        return _decision("vm", "model_vm", "The local model did not call this a plain question, so it goes to the guest.", ())
    return _decision("vm", "unsure", "It is not clear that this is only a question, so it goes to the guest.", ())


def _decision(route: str, code: str, reason: str, services: tuple[str, ...]) -> RouteDecision:
    source = "override" if code.startswith("override") else "model" if code.startswith("model") else "default" if code == "unsure" else "rule"
    return RouteDecision(route=route, reason=reason, reason_code=code, source=source, services=services)

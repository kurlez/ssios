"""Transparent route classifier.

Rules decide first. A plain question, in Chinese or English, stays on local
Ollama. A request that asks for files, commands, a workspace edit, or a tool
goes to the guest. The local model breaks a tie only when the message looks
like that kind of task and the rules are not sure. That tie defaults to the
guest. Anything else stays direct. Overrides are explicit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ssios.calendar_intent import creative_request, general_question, mentions_calendar

_QUESTION = re.compile(
    r"^(what|why|how|when|who|where|explain|describe|hello|hi|thanks|thank you)\b",
    re.IGNORECASE,
)
_IDENTITY = re.compile(
    r"^(who are you|what are you|what can you|what do you|how are you)\b",
    re.IGNORECASE,
)
_CN_QUESTION = (
    "你是谁",
    "你会什么",
    "你能做什么",
    "你是什么",
    "什么是",
    "为什么",
    "怎么",
    "如何",
    "哪里",
    "哪个",
    "哪些",
    "多少",
    "是不是",
    "吗",
    "呢",
)
_CN_TASK = ("运行", "执行", "编译", "修改", "编辑", "改一下", "写一个文件", "删除文件")
_TASK_NOUN = re.compile(
    r"\b(file|files|workspace|readme|script|command|terminal|folder|directory|code)\b",
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
    r"\b(gmail|inbox|e-mail|email|unread|google drive|my drive|drive file|calendar|calendars)\b",
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
    skill: str = ""

    def public(self) -> dict:
        payload = {
            "route": self.route,
            "reason": self.reason,
            "source": self.source,
            "services": list(self.services),
        }
        if self.skill:
            payload["skill"] = self.skill
        return payload


def classify(text: str, override: str = "auto", llm=None, deadline: float | None = None) -> RouteDecision:
    from ssios.skill_broker import match_skill

    message = " ".join(str(text or "").split())
    services = _services(message)
    forced = override if override in {"force_direct", "force_vm", "auto"} else "auto"
    from ssios.goals import task_command
    from ssios.reminders import is_agenda, reminder_command, wants_forget, wants_remember

    from ssios.calendar_intent import no_tool_request

    if no_tool_request(message) and forced != "force_vm":
        return _decision("direct", "rule_direct", "The message asks for writing or an explanation, so the local model answers directly.", ())
    skill = match_skill(message)
    if task_command(message) and forced != "force_vm":
        return _decision("tasks", "rule_tasks", "The message is a task command. It is handled here, not by the model.", ())
    if reminder_command(message) and forced != "force_vm":
        return _decision("reminders", "rule_reminders", "The message is a reminder command. The time is computed on this computer.", ())
    if wants_forget(message) and forced != "force_vm":
        return _decision("memory", "rule_memory", "The message asks to forget a saved fact. It is not removed until you approve it.", ())
    if wants_remember(message) and forced != "force_vm":
        return _decision("memory", "rule_memory", "The message asks to save memory. A timed reminder is stored immediately. A note waits for approval.", ())
    if is_agenda(message) and forced != "force_vm" and "calendar" not in services:
        return _decision("schedule", "rule_schedule", "The message asks what is planned. Reminders and the calendar are read. The model is not asked to invent them.", ())
    if skill and forced != "force_vm" and not _writes(message):
        return _decision(
            "skill",
            "rule_skill",
            "The message asks for a public web read. It runs on this computer. The guest stays offline.",
            (),
            skill,
        )
    if forced == "force_vm":
        if services:
            return _decision("connector_vm", "override_vm", "You chose the guest. Cloud data is read on this machine. A workspace change still waits for approval.", services)
        return _decision("vm", "override_vm", "You chose the guest for this message.", ())
    if forced == "force_direct":
        if services:
            return _decision("connector", "override_direct", "You chose a direct answer. Cloud data is read on this machine. A calendar change still waits for approval.", services)
        if _saved_facts(message):
            return _decision("account", "override_direct", "You chose a direct answer. Saved facts are not invented.", ())
        if _REMEMBER.search(message):
            return _decision("memory", "override_direct", "You chose not to use the guest. A memory write still waits for your approval.", ())
        return _decision("direct", "override_direct", "You chose a direct answer. No tools will run.", ())
    if services and (_writes(message) or _EXECUTE.search(message)):
        return _decision(
            "connector_vm",
            "rule_connector_vm",
            "The message needs cloud data and a workspace change. The read runs on this machine. The guest result still waits for approval.",
            services,
        )
    if services and not (_DEFINITION.search(message) and "my" not in message.casefold() and "inbox" not in message.casefold()):
        return _decision("connector", "rule_connector", _cloud_reason(services), services)
    if _REMEMBER.search(message):
        return _decision("memory", "rule_memory", "The message asks to save memory. It is not written until you approve it.", ())
    saved = _saved_facts(message)
    if saved:
        return _decision(
            "account",
            "rule_account",
            "The message asks for saved facts on this computer. The local model will not invent them.",
            (),
        )
    if _clear_task(message):
        return _decision("vm", "rule_vm", "The message asks to run code or change a workspace, so it goes to the guest.", ())
    if _is_question(message):
        return _decision("direct", "rule_direct", "The message is a question, so the local model answers directly.", ())
    if _taskish(message):
        return _unsure(message, llm, deadline)
    return _model_or_chat(message, llm, deadline)


def _model_or_chat(message: str, llm, deadline: float | None) -> RouteDecision:
    """Ambiguous text may use the small classifier. A timeout or a low score stays on local chat."""
    reader = getattr(llm, "classify_route", None)
    if not callable(reader):
        return _decision("direct", "rule_direct", "The message does not ask for files, commands, or a workspace change, so the local model answers directly.", ())
    try:
        payload = reader(message, deadline)
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return _decision("direct", "rule_direct", "The classifier did not answer, so this stays a local chat.", ())
    from ssios.route_classifier import decision_from_classifier

    chosen = decision_from_classifier(payload)
    if chosen is None or chosen.route == "direct":
        return _decision("direct", "rule_direct", "The classifier was not confident, so this stays a local chat.", ())
    return chosen


def _writes(message: str) -> bool:
    return bool(_WRITE_FILE.search(message) or _WRITE.search(message))


def _clear_task(message: str) -> bool:
    if _writes(message) or _EXECUTE.search(message):
        return True
    return any(cue in message for cue in _CN_TASK)


def _is_question(message: str) -> bool:
    if message.endswith("?") or message.endswith("？"):
        return True
    if _QUESTION.search(message) or _IDENTITY.search(message):
        return True
    return any(cue in message for cue in _CN_QUESTION)


def _taskish(message: str) -> bool:
    if _clear_task(message) or _TASK_NOUN.search(message):
        return True
    return any(cue in message for cue in ("文件", "代码", "工作区", "脚本", "终端"))


def _saved_facts(message: str) -> str:
    """Questions about memory or reminders. Empty when the message is not one of those."""
    folded = message.casefold()
    if any(cue in message for cue in ("我记住了什么", "我记得什么", "我的记忆")) or re.search(
        r"\b(what do you remember|show my memory|my memory)\b", folded
    ):
        return "memory"
    if any(cue in message for cue in ("我的待办", "有什么待办", "待办事项")) or re.search(
        r"\b(my tasks|my reminders|open tasks|open reminders)\b", folded
    ):
        return "tasks"
    return ""


def _services(message: str) -> tuple[str, ...]:
    folded = message.casefold()
    if creative_request(message) or general_question(message):
        return ()
    if _DEFINITION.search(message) and "my" not in folded and "inbox" not in folded and "unread" not in folded:
        return ()
    found = []
    if any(word in folded for word in ("gmail", "inbox", "e-mail", "email", "unread")) or any(
        word in message for word in ("邮箱", "邮件", "发件人", "收件箱", "寄件人")
    ):
        found.append("gmail")
    if any(word in folded for word in ("google drive", "my drive", "drive file")) or any(word in message for word in ("云端硬盘", "网盘", "根目录")) or re.search(r"\bdrive\b", folded):
        found.append("drive")
    if mentions_calendar(message):
        found.append("calendar")
    return tuple(found)


def _cloud_reason(services: tuple[str, ...]) -> str:
    if services == ("calendar",):
        return "The message asks for Google Calendar. A read runs now. A change waits for approval."
    if "calendar" in services:
        return "The message asks for Google Calendar and other cloud data. Reads run now. A change waits for approval."
    return "The message asks for Gmail or Drive. The read runs on this machine."


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
        return _decision("vm", "model_vm", "The local model treated this as a file or workspace task, so it goes to the guest.", ())
    return _decision("vm", "unsure", "The message looks like a file or workspace task, so it goes to the guest.", ())


def _decision(route: str, code: str, reason: str, services: tuple[str, ...], skill: str = "") -> RouteDecision:
    source = "override" if code.startswith("override") else "model" if code.startswith("model") else "default" if code == "unsure" else "rule"
    return RouteDecision(route=route, reason=reason, reason_code=code, source=source, services=services, skill=skill)

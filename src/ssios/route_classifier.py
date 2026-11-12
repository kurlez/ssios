"""A small local classifier. It labels a turn. It does not call tools or approve anything."""

from __future__ import annotations

import json
import re

from ssios.chat_router import RouteDecision

INTENTS = (
    "chat",
    "creative",
    "explain",
    "translate",
    "rewrite",
    "calendar",
    "gmail",
    "drive",
    "weather",
    "web",
    "codex",
    "memory",
    "tasks",
)
NO_TOOL = {"chat", "creative", "explain", "translate", "rewrite"}
CONFIDENT = 0.7

CLASSIFIER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "tools", "confidence"],
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "tools": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
}

CLASSIFIER_SYSTEM = (
    "Classify one user message. Return JSON only. Do not answer the user. "
    "intent is chat, creative, explain, translate, rewrite, calendar, gmail, drive, weather, web, codex, memory, or tasks. "
    "Creative writing, explanation, translation, rewriting, and general chat use intent chat or creative and tools []. "
    "tools lists a tool only when the user asks to use their mail, calendar, files, or the web. "
    "confidence is a number from 0 to 1."
)


def parse_classifier(content: str) -> dict | None:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    intent = str(data.get("intent") or "")
    if intent not in INTENTS:
        return None
    tools = data.get("tools")
    if not isinstance(tools, list):
        tools = []
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        return None
    return {"intent": intent, "tools": [str(item) for item in tools if str(item)], "confidence": confidence}


def decision_from_classifier(payload: dict | None) -> RouteDecision | None:
    """Map a classifier object to a route. Low confidence and no-tool intents stay on local chat."""
    if not isinstance(payload, dict):
        return None
    intent = str(payload.get("intent") or "")
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError):
        return None
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    if intent not in INTENTS or confidence < CONFIDENT:
        return _chat("The classifier was not confident, so this stays a local chat.")
    if intent in NO_TOOL or not tools and intent in {"chat", "creative", "explain", "translate", "rewrite"}:
        return _chat("The message needs no tool, so the local model answers.")
    if intent == "weather":
        return RouteDecision("skill", "The classifier chose a weather read on this computer.", "model_skill", "model", skill="weather")
    if intent == "web":
        return RouteDecision("skill", "The classifier chose a public web read on this computer.", "model_skill", "model", skill="web_search")
    if intent == "gmail":
        return RouteDecision("connector", "The classifier chose Gmail. The read runs on this machine.", "model_connector", "model", services=("gmail",))
    if intent == "calendar":
        return RouteDecision("connector", "The classifier chose Google Calendar. A read runs now. A change waits for approval.", "model_connector", "model", services=("calendar",))
    if intent == "drive":
        return RouteDecision("connector", "The classifier chose Drive. The read runs on this machine.", "model_connector", "model", services=("drive",))
    if intent == "memory":
        return RouteDecision("memory", "The classifier chose a memory command. It is not written until you approve it.", "model_memory", "model")
    if intent == "tasks":
        return RouteDecision("tasks", "The classifier chose a task command. It is handled here.", "model_tasks", "model")
    if intent == "codex" and tools:
        return RouteDecision("vm", "The classifier chose tools, so this can use the guest.", "model_vm", "model")
    return _chat("The classifier did not name a tool, so this stays a local chat.")


def _chat(reason: str) -> RouteDecision:
    return RouteDecision("direct", reason, "rule_direct", "model")

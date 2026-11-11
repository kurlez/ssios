"""Per-account MemFS reads and explicit-fact proposals.

One git repository per account is shared by every conversation and every route.
The default mode ``letta-memfs-compat`` is this repository's git markdown
library. It is not the upstream Letta process. Upstream mode is opt-in and
does not start unless that CLI is installed.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta

from ssios.errors import SsiosError
from ssios.util import read_json, write_json

_CORE = ("human.md", "persona.md", "project.md", "MEMORY.md", "preferences.md")
_CORE_META = {
    "human.md": ("human", "profile"),
    "persona.md": ("persona", "persona"),
    "preferences.md": ("preferences", "preferences"),
    "project.md": ("project", "account project"),
}


def write_mode(home) -> str:
    data = read_json(home / "memory-prefs.json", {"mode": "approve"})
    mode = str(data.get("mode") or "approve") if isinstance(data, dict) else "approve"
    return mode if mode in {"approve", "auto"} else "approve"


def set_write_mode(home, mode: str) -> None:
    if mode not in {"approve", "auto"}:
        raise SsiosError("memory_invalid", "memory write mode rejected", 400)
    write_json(home / "memory-prefs.json", {"mode": mode})


def memory_context(scope, query: str = "") -> tuple[str, list[str]]:
    """Core blocks plus topic notes that match the query. Templates are skipped."""
    from ssios.llm import is_template_boilerplate

    store = scope.memory()
    project = scope.project_id()
    parts: list[str] = []
    used: list[str] = []
    for rel in _CORE:
        text = _read(store, project, rel)
        if not text:
            continue
        body = _body(text).strip()
        has_fact = bool(re.search(r"(?m)^-\s+[A-Za-z0-9_-]+:\s+\S", body))
        if is_template_boilerplate(rel, text) and not has_fact:
            continue
        if not body or body in {"# Memory", "# Fixtures"} or body.startswith("# Synthetic memory index"):
            continue
        parts.append(f"# {rel}\n{body[:800]}")
        used.append(rel)
    needle = _needle(query)
    if needle:
        try:
            found = store.search(project, needle, include_untrusted=False)
        except SsiosError:
            found = {"hits": []}
        for hit in found.get("hits") or []:
            path = str(hit.get("path") or "")
            if not path.startswith("topics/") or path in used:
                continue
            text = _read(store, project, path)
            if not text:
                continue
            parts.append(f"# {path}\n{_body(text).strip()[:800]}")
            used.append(path)
            if len([item for item in used if item.startswith("topics/")]) >= 3:
                break
    scope._last_memory_files = list(used)
    return "\n\n".join(parts)[:4000], used


_STATEMENT = re.compile(
    r"\b(my name is|i am called|i prefer|i like|i live in|i'm in|i am in|i work at|i use)\b|我叫|我的名字是|我喜欢|我偏好|我住在|我在",
    re.IGNORECASE,
)


def stored_location(scope) -> str:
    """The Location bullet in human.md, or empty when the account has not saved one."""
    text = _read(scope.memory(), scope.project_id(), "human.md")
    for line in _body(text).splitlines():
        match = re.match(r"^-\s+(?:Location|lives):\s*(.+)$", line.strip(), re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


def stored_name(scope) -> str:
    """The name bullet in human.md, or empty when the account has not saved one."""
    text = _read(scope.memory(), scope.project_id(), "human.md")
    for line in _body(text).splitlines():
        match = re.match(r"^-\s+name:\s*(.+)$", line.strip(), re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


def name_question(message: str) -> bool:
    folded = str(message or "").casefold().rstrip("？?").strip()
    return folded in {
        "我叫什么",
        "我叫什么名字",
        "我的名字是什么",
        "我的名字叫什么",
        "what is my name",
        "what's my name",
        "whats my name",
    }


def collect_facts(message: str, llm=None) -> list[dict]:
    """Regex facts plus a validated model extract. Only the user message is passed in."""
    facts = explicit_facts(message)
    seen = {(item["path"], item["key"]) for item in facts}
    for item in model_facts(message, llm):
        key = (item["path"], item["key"])
        if key in seen:
            continue
        facts.append(item)
        seen.add(key)
    return facts


def model_facts(message: str, llm) -> list[dict]:
    """Structured extract. A value is kept only when the user explicitly stated it."""
    text = " ".join(str(message or "").split())
    if llm is None or not _STATEMENT.search(text):
        return []
    if getattr(llm, "provider", "") == "mock" and not getattr(llm, "fact_script", None):
        return []
    raw = _ask_facts(llm, text)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    items = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return _validate_facts(text, items)


def _ask_facts(llm, text: str) -> str:
    import threading
    import time

    messages = [
        {
            "role": "system",
            "content": (
                "FACT-EXTRACT. Return JSON only: "
                '{"facts":[{"path":"human.md or preferences.md","key":"short","value":"words from the user"}]}. '
                "Include a fact only when this message explicitly states it. Otherwise return {\"facts\":[]}."
            ),
        },
        {"role": "user", "content": text[:1000]},
    ]
    try:
        return str(llm.complete_chat(messages, cancel_event=threading.Event(), deadline=time.monotonic() + 8))
    except (SsiosError, OSError, TypeError, ValueError):
        return ""


def _validate_facts(message: str, items: list) -> list[dict]:
    folded = message.casefold()
    kept = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        key = str(item.get("key") or "")
        value = " ".join(str(item.get("value") or "").split())
        if path not in {"human.md", "preferences.md"}:
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", key):
            continue
        if not 2 <= len(value) <= 80 or value.casefold() not in folded:
            continue
        kept.append({"path": path, "key": key, "value": value})
    return kept


def snapshot_files(scope, paths: list[str]) -> list[dict]:
    store = scope.memory()
    project = scope.project_id()
    return [{"path": path, "content": _read(store, project, path)} for path in paths]


def push_undo(home, files: list[dict]) -> str:
    data = read_json(home / "memory-undo.json", {"items": []})
    items = data.get("items") if isinstance(data, dict) else []
    if not isinstance(items, list):
        items = []
    undo_id = "memu-" + secrets.token_hex(4)
    items.append({"id": undo_id, "files": files})
    write_json(home / "memory-undo.json", {"items": items[-20:]})
    return undo_id


def take_undo(home, undo_id: str) -> list[dict] | None:
    data = read_json(home / "memory-undo.json", {"items": []})
    items = data.get("items") if isinstance(data, dict) else []
    if not isinstance(items, list):
        return None
    found = None
    rest = []
    for item in items:
        if isinstance(item, dict) and item.get("id") == undo_id and found is None:
            found = item
        else:
            rest.append(item)
    if found is None:
        return None
    write_json(home / "memory-undo.json", {"items": rest})
    files = found.get("files") if isinstance(found.get("files"), list) else []
    return [item for item in files if isinstance(item, dict) and item.get("path")]


def explicit_facts(message: str) -> list[dict]:
    """Facts the user stated. Tool text is never passed here."""
    text = " ".join(str(message or "").split())
    facts = []
    name = re.search(
        r"(?:my name is|i am called|我的名字是|我叫)\s*([A-Za-z][A-Za-z0-9'_-]{0,40}|[\u4e00-\u9fff]{1,8})",
        text,
        re.IGNORECASE,
    )
    if name and name.group(1).casefold() not in {"what", "who", "什么", "啥"}:
        facts.append({"path": "human.md", "key": "name", "value": name.group(1).strip(" .")})
    prefer = re.search(r"(?:i prefer|i like|我喜欢|我偏好)\s+([^。.]{1,80})", text, re.IGNORECASE)
    if prefer:
        facts.append({"path": "preferences.md", "key": "prefers", "value": prefer.group(1).strip(" .")})
    home = re.search(r"(?:i live in|i'm in|i am in|我住在|我在)\s*([^。.?？!]{1,80})", text, re.IGNORECASE)
    if home:
        value = home.group(1).strip(" .")
        if value and value[0] not in set("想看做说听写吃走读"):
            facts.append({"path": "human.md", "key": "Location", "value": value})
    return facts


def files_for_facts(scope, facts: list[dict]) -> list[dict]:
    """Merge facts into core files. The same key is updated, not duplicated."""
    if not facts:
        return []
    store = scope.memory()
    project = scope.project_id()
    grouped: dict[str, dict[str, str]] = {}
    for fact in facts:
        grouped.setdefault(str(fact["path"]), {})[str(fact["key"])] = str(fact["value"])
    files = []
    for path, updates in grouped.items():
        current = _read(store, project, path) or ""
        name, description = _CORE_META.get(path, ("note", "note"))
        content = _merge_core(current, name, description, updates)
        if content.strip() == current.strip():
            continue
        files.append({"path": path, "content": content})
    return files


def note_document(body: str, when: datetime) -> dict:
    from ssios.vault import slugify

    text = body.strip()
    if when.tzinfo is not None:
        text = _with_absolute_dates(text, when)
    slug = slugify(text)[:40] or "note"
    path = f"topics/chat/{when.date().isoformat()}-{slug}.md"
    content = f"---\nname: chat-note\ndescription: \"approved chat memory\"\n---\n\n{text}\n"
    return {"path": path, "content": content}


def _with_absolute_dates(text: str, when: datetime) -> str:
    day = when.date()
    replacements = {
        "明天": (day + timedelta(days=1)).isoformat(),
        "今天": day.isoformat(),
        "后天": (day + timedelta(days=2)).isoformat(),
    }
    notes = []
    lowered = text.casefold()
    if "tomorrow" in lowered:
        notes.append("tomorrow=" + (day + timedelta(days=1)).isoformat())
    if re.search(r"\btoday\b", lowered):
        notes.append("today=" + day.isoformat())
    for word, stamp in replacements.items():
        if word in text:
            notes.append(f"{word}={stamp}")
    if not notes:
        return text
    return text + "\n\nAbsolute dates: " + ", ".join(notes) + "."


def _merge_core(current: str, name: str, description: str, updates: dict[str, str]) -> str:
    bullets: dict[str, str] = {}
    for line in _body(current).splitlines():
        match = re.match(r"^-\s+([A-Za-z0-9_-]+):\s*(.*)$", line.strip())
        if match:
            bullets[match.group(1)] = match.group(2).strip()
    bullets.update(updates)
    body = "\n".join(f"- {key}: {value}" for key, value in bullets.items())
    return f"---\nname: {name}\ndescription: \"{description}\"\n---\n\n{body}\n"


def _body(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end < 0:
        return text
    return text[end + 5 :]


def _read(store, project: str, rel: str) -> str:
    try:
        doc = store.read_file(project, rel, include_untrusted=False)
    except SsiosError:
        return ""
    if doc.get("tombstone"):
        return ""
    return str(doc.get("content") or "")


def _needle(query: str) -> str:
    words = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{3,}", str(query or ""))
    if not words:
        return ""
    words.sort(key=len, reverse=True)
    return words[0][:80]

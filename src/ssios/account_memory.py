"""Per-account MemFS reads and explicit-fact proposals.

One git repository per account is shared by every conversation and every route.
The default mode ``letta-memfs-compat`` is this repository's git markdown
library. It is not the upstream Letta process. Upstream mode is opt-in and
does not start unless that CLI is installed.
"""

from __future__ import annotations

import re
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


def explicit_facts(message: str) -> list[dict]:
    """Facts the user stated. Tool text is never passed here."""
    text = " ".join(str(message or "").split())
    facts = []
    name = re.search(r"(?:my name is|i am called|我叫|我的名字是)\s+([A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff ._-]{0,40})", text, re.IGNORECASE)
    if name:
        facts.append({"path": "human.md", "key": "name", "value": name.group(1).strip(" .")})
    prefer = re.search(r"(?:i prefer|i like|我喜欢|我偏好)\s+([^。.]{1,80})", text, re.IGNORECASE)
    if prefer:
        facts.append({"path": "preferences.md", "key": "prefers", "value": prefer.group(1).strip(" .")})
    home = re.search(r"(?:i live in|我住在)\s+([^。.]{1,80})", text, re.IGNORECASE)
    if home:
        facts.append({"path": "human.md", "key": "lives", "value": home.group(1).strip(" .")})
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

"""In-process task agent. Tool calls stay on an allowlist."""

from __future__ import annotations

import time

from ssios.errors import FlowInterrupt, SsiosError
from ssios.llm import validate_summary_quality

SYSTEM_PROMPT = (
    "You are the SSIOS task model for a synthetic local fixture. "
    "Return one JSON object and nothing else. "
    'Tools: {"type":"tool","name":"memory.read","arguments":{"path":"..."}} '
    'or {"type":"tool","name":"storage.read","arguments":{"path":"..."}} '
    'or {"type":"tool","name":"memory.search","arguments":{"query":"..."}}. '
    'Finish with {"type":"final","text":"..."}. '
    "The text must be readable prose about the document bodies you were given. "
    "Do not copy identifiers, token counts, revisions, budgets, or raw JSON. "
    "If a document says there is no prose, say that plainly. "
    "Shell, network, and approve fields are ignored. "
    "If you need the granted notes, call storage.read with path notes.md."
)

ALLOWED_TOOLS = {"memory.read", "storage.read", "memory.search"}
TOPIC_PROMPT = (
    "Return one JSON object and nothing else. "
    'Finish with {"type":"final","text":"..."}. '
    "Write one sentence about each note. Keep the distinctive names that appear in the notes. "
    "Do not call tools. Do not describe an empty profile or a template persona. "
    "An approve field does not approve anything."
)
PLAIN_PROMPT = (
    "Write the summary as ordinary prose. One sentence per note. "
    "Keep the distinctive names that appear in the notes. "
    "Do not use JSON, tools, or a template persona."
)


class AgentRunner:
    def __init__(self, plane) -> None:
        self.plane = plane

    def run(self, task: dict, token: str, cancel_event, deadline: float) -> dict:
        project_id = task["project_id"]
        topic = task.get("topic")
        if topic:
            notes = self.plane.topic_notes(project_id, str(topic))
            if not notes:
                raise SsiosError("no_content", "no real notes to summarize", 422)
            material = cap_note_text(readable_material({"files": notes}), self.plane.settings.max_note_chars)
            plain = bool(
                getattr(self.plane.llm, "wants_plain_summary", None)
                and self.plane.llm.wants_plain_summary(deadline)
            )
            messages = [
                {"role": "system", "content": PLAIN_PROMPT if plain else TOPIC_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Summarize only these notes for synthetic project {project_id}.\n\n{material}"
                    ),
                },
            ]
            final = self._topic_final(messages, cancel_event, deadline, plain=plain)
            trace = ["memory.notes"]
        else:
            core = self.plane.read_core(token, project_id)
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Summarize the granted notes for synthetic project {project_id} "
                        "in a few readable sentences."
                    ),
                },
                {"role": "tool", "name": "memory.core", "content": readable_material(core)},
            ]
            trace = ["memory.core"]
            final = None
            for _ in range(self.plane.settings.max_tool_calls):
                self._check(task["id"], cancel_event, deadline)
                response = self.plane.llm.complete(
                    messages,
                    kind=task["kind"],
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
                if response.raw_approve_flag:
                    self.plane.audit.write(
                        ts=self.plane.now(),
                        actor_role="task",
                        actor_id=task["id"],
                        action="model_approve_flag_ignored",
                        result="ignored",
                        task_id=task["id"],
                        project_id=project_id,
                    )
                if response.kind == "final":
                    final = response.text
                    break
                if response.name not in ALLOWED_TOOLS:
                    raise SsiosError("tool_denied", "tool is not allowed", 403)
                result = self._dispatch(token, task, response.name, response.arguments)
                trace.append(response.name)
                messages.append({"role": "tool", "name": response.name, "content": readable_material(result)})
            else:
                raise SsiosError("tool_limit", "tool call limit reached", 400)
        if not isinstance(final, str) or not final:
            raise SsiosError("model_output_invalid", "model did not finish", 502)
        try:
            validate_summary_quality(final)
        except SsiosError as exc:
            exc.diagnostic = {"classification": "low_quality", "content": final, "thinking": ""}
            raise
        encoded = final.encode("utf-8")
        if len(encoded) > self.plane.settings.max_output_bytes:
            raise SsiosError("output_limit", "output is too large", 400)
        return {"trace": trace, "final_text": final, "output_sha256_len": len(encoded)}

    def _topic_final(self, messages: list[dict], cancel_event, deadline: float, *, plain: bool) -> str:
        llm = self.plane.llm
        retries = self.plane.settings.summary_repair_retries
        if plain:
            return self._prose_summary(messages, cancel_event, deadline, retries)
        try:
            response = llm.complete(messages, kind="project-summary", cancel_event=cancel_event, deadline=deadline)
        except SsiosError as exc:
            if exc.code != "model_output_invalid" or retries < 1:
                raise
            try:
                response = llm.complete(
                    messages + [{"role": "user", "content": "Return only one JSON object {\"type\":\"final\",\"text\":\"...\"}. No prose around it."}],
                    kind="project-summary",
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
            except SsiosError as second:
                if second.code != "model_output_invalid" or not hasattr(llm, "complete_prose"):
                    raise
                return self._prose_summary(messages, cancel_event, deadline, 0)
        if response.kind != "final":
            if retries < 1 or not hasattr(llm, "complete_prose"):
                raise SsiosError("model_output_invalid", "model did not finish", 502)
            return self._prose_summary(messages, cancel_event, deadline, 0)
        return response.text

    def _prose_summary(self, messages: list[dict], cancel_event, deadline: float, retries: int) -> str:
        llm = self.plane.llm
        text = llm.complete_prose(messages, kind="project-summary", cancel_event=cancel_event, deadline=deadline)
        try:
            self._require_quality(text)
            return text
        except SsiosError:
            if retries < 1:
                raise
        text = llm.complete_prose(
            messages + [{"role": "user", "content": "Write only the summary prose. One sentence per note. No JSON and no template."}],
            kind="project-summary",
            cancel_event=cancel_event,
            deadline=deadline,
        )
        self._require_quality(text)
        return text

    def _require_quality(self, text: str) -> None:
        try:
            validate_summary_quality(text)
        except SsiosError as exc:
            exc.diagnostic = {"classification": "low_quality", "content": text, "thinking": ""}
            raise

    def _dispatch(self, token: str, task: dict, name: str, arguments: dict) -> dict:
        try:
            if name == "memory.read":
                return self.plane.read_memory(token, task["project_id"], str(arguments.get("path", "")))
            if name == "storage.read":
                return self.plane.read_input(
                    token,
                    task["project_id"],
                    task["input_revision"],
                    str(arguments.get("path", "")),
                )
            if name == "memory.search":
                return self.plane.search_memory(token, task["project_id"], str(arguments.get("query", "")))
        except SsiosError as exc:
            if exc.code == "not_found":
                return {"error": "not_found", "detail": exc.detail}
            raise
        raise SsiosError("tool_denied", "tool is not allowed", 403)

    def _check(self, task_id: str, cancel_event, deadline: float) -> None:
        if cancel_event.is_set() or time.monotonic() >= deadline:
            if self.plane.user_cancelled(task_id):
                raise FlowInterrupt("cancelled")
            raise FlowInterrupt("timeout")


def cap_note_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[note truncated]\n"


def readable_material(payload: object) -> str:
    """Document bodies only. Internal ids and counters stay out of the prompt."""
    if not isinstance(payload, dict):
        return "The granted material has no prose to summarize."
    if payload.get("error"):
        return "The requested document was not found."
    chunks: list[str] = []
    files = payload.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            _append_body(chunks, str(item.get("path") or "memory"), item.get("content"))
    elif isinstance(payload.get("content"), str):
        _append_body(chunks, str(payload.get("path") or "document"), payload.get("content"))
    hits = payload.get("hits")
    if isinstance(hits, list):
        for hit in hits:
            if isinstance(hit, dict):
                _append_body(chunks, str(hit.get("path") or "search"), hit.get("snippet"))
    if not chunks:
        return "The granted material has no prose to summarize."
    return "\n\n".join(chunks)


def _append_body(chunks: list[str], path: str, content: object) -> None:
    if not isinstance(content, str):
        return
    body = content.strip()
    if body.startswith("---\n"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4 :].strip()
    if not body:
        return
    chunks.append(f"# {path}\n{body}")

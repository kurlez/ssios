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


class AgentRunner:
    def __init__(self, plane) -> None:
        self.plane = plane

    def run(self, task: dict, token: str, cancel_event, deadline: float) -> dict:
        project_id = task["project_id"]
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
        validate_summary_quality(final)
        encoded = final.encode("utf-8")
        if len(encoded) > self.plane.settings.max_output_bytes:
            raise SsiosError("output_limit", "output is too large", 400)
        return {"trace": trace, "final_text": final, "output_sha256_len": len(encoded)}

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
    if not body:
        return
    chunks.append(f"# {path}\n{body}")

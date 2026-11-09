"""In-process task agent. Tool calls stay on an allowlist."""

from __future__ import annotations

import json
import time

from ssios.errors import FlowInterrupt, SsiosError

SYSTEM_PROMPT = (
    "You are the SSIOS task model for a synthetic local fixture. "
    "Return one JSON object and nothing else. "
    'Tools: {"type":"tool","name":"memory.read","arguments":{"path":"..."}} '
    'or {"type":"tool","name":"storage.read","arguments":{"path":"..."}} '
    'or {"type":"tool","name":"memory.search","arguments":{"query":"..."}}. '
    'Finish with {"type":"final","text":"..."}. '
    "Core memory is already loaded. Shell, network, and approve fields are ignored. "
    "If you need the granted input, call storage.read with path notes.md. "
    "Reasoning may sit inside <think></think>; the JSON object must be outside those tags. "
    "An approve field does not approve anything."
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
                "content": f"Summarize the granted input for synthetic project {project_id}.",
            },
            {"role": "tool", "name": "memory.core", "content": json.dumps(core, ensure_ascii=False)},
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
            messages.append(
                {"role": "tool", "name": response.name, "content": json.dumps(result, ensure_ascii=False)}
            )
        else:
            raise SsiosError("tool_limit", "tool call limit reached", 400)
        if not isinstance(final, str) or not final:
            raise SsiosError("model_output_invalid", "model did not finish", 502)
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

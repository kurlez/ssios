"""Model adapters. The mock provider is deterministic and does not use the network."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ssios.errors import FlowInterrupt, SsiosError

MARKERS = ("CORE-PROJECT-FACT", "THREE-BULLET-RULE", "NORTHWIND-FIXTURE-NOTE")


@dataclass
class ModelResponse:
    kind: str
    name: str = ""
    arguments: dict = field(default_factory=dict)
    text: str = ""
    raw_approve_flag: bool = False


def parse_model_payload(text: str) -> ModelResponse:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise SsiosError("model_output_invalid", "model output is not JSON", 502)
    try:
        obj = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SsiosError("model_output_invalid", "model output is not JSON", 502) from exc
    if not isinstance(obj, dict):
        raise SsiosError("model_output_invalid", "model output is not an object", 502)
    approve = bool(obj.get("approve"))
    if obj.get("type") == "final":
        return ModelResponse(kind="final", text=str(obj.get("text", "")), raw_approve_flag=approve)
    if obj.get("type") == "tool":
        arguments = obj.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise SsiosError("model_output_invalid", "tool arguments rejected", 502)
        return ModelResponse(
            kind="tool",
            name=str(obj.get("name", "")),
            arguments=arguments,
            raw_approve_flag=approve,
        )
    raise SsiosError("model_output_invalid", "model output type rejected", 502)


class MockLLM:
    provider = "mock"
    deterministic = True
    uses_network = False

    def complete(self, messages: list[dict], *, kind: str, cancel_event, deadline: float) -> ModelResponse:
        if kind == "slow":
            while True:
                if cancel_event.is_set() or time.monotonic() >= deadline:
                    raise FlowInterrupt("interrupted")
                time.sleep(0.05)
        if kind == "shell-attempt":
            return ModelResponse(kind="tool", name="shell", arguments={"command": "id"}, raw_approve_flag=True)
        if kind != "project-summary":
            raise SsiosError("task_rejected", "mock task kind rejected", 400)
        blob = json.dumps(messages, ensure_ascii=False)
        if "THREE-BULLET-RULE" not in blob:
            return ModelResponse(kind="tool", name="memory.read", arguments={"path": "topics/fixtures/overview.md"})
        if "NORTHWIND-FIXTURE-NOTE" not in blob:
            return ModelResponse(kind="tool", name="storage.read", arguments={"path": "notes.md"})
        missing = [marker for marker in MARKERS if marker not in blob]
        if missing:
            raise SsiosError("mock_missing_context", "mock context is incomplete", 500)
        text = "Synthetic project summary\n" + "\n".join(f"- {marker}" for marker in MARKERS)
        return ModelResponse(kind="final", text=text, raw_approve_flag=True)


class OllamaLLM:
    provider = "ollama"
    deterministic = False
    uses_network = True

    def __init__(self, base_url: str, model: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def complete(self, messages: list[dict], *, kind: str, cancel_event, deadline: float) -> ModelResponse:
        del cancel_event, deadline
        if kind != "project-summary":
            raise SsiosError("task_rejected", "this task kind requires the mock provider", 400)
        payload = {
            "model": self.model,
            "messages": _to_ollama(messages),
            "stream": False,
            "options": {"temperature": 0},
        }
        request = urllib.request.Request(
            self.base_url + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SsiosError("llm_unavailable", f"ollama http {exc.code}", 503) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise SsiosError("llm_unavailable", "ollama is unreachable", 503) from exc
        if not isinstance(body, dict) or "message" not in body:
            raise SsiosError("llm_unavailable", "ollama response is incomplete", 503)
        content = body.get("message", {}).get("content", "")
        return parse_model_payload(str(content))


def _to_ollama(messages: list[dict]) -> list[dict]:
    converted = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content", ""))
        if role == "tool":
            converted.append({"role": "user", "content": f"TOOL_RESULT {message.get('name', '')}\n{content}"})
        elif role in ("system", "user", "assistant"):
            converted.append({"role": role, "content": content})
    return converted


def build_llm(settings):
    if settings.llm_provider == "mock":
        return MockLLM()
    if settings.llm_provider == "ollama":
        return OllamaLLM(settings.ollama_base_url, settings.ollama_model, settings.llm_timeout_seconds)
    raise SsiosError("config_invalid", "llm provider rejected", 500)

"""Model adapters. The mock provider is deterministic and does not use the network."""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ssios.errors import FlowInterrupt, SsiosError

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think>", re.IGNORECASE)

MARKERS = ("CORE-PROJECT-FACT", "THREE-BULLET-RULE", "NORTHWIND-FIXTURE-NOTE")


@dataclass
class ModelResponse:
    kind: str
    name: str = ""
    arguments: dict = field(default_factory=dict)
    text: str = ""
    raw_approve_flag: bool = False


def parse_model_payload(text: str) -> ModelResponse:
    visible = _strip_think(text)
    visible = _strip_fence(visible)
    chosen = None
    for obj in _json_objects(visible):
        if obj.get("type") in ("final", "tool"):
            chosen = obj
    if chosen is None:
        raise SsiosError("model_output_invalid", "model output is not JSON", 502)
    approve = bool(chosen.get("approve"))
    if chosen.get("type") == "final":
        return ModelResponse(kind="final", text=str(chosen.get("text", "")), raw_approve_flag=approve)
    arguments = chosen.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise SsiosError("model_output_invalid", "tool arguments rejected", 502)
    return ModelResponse(
        kind="tool",
        name=str(chosen.get("name", "")),
        arguments=arguments,
        raw_approve_flag=approve,
    )


def _strip_think(text: str) -> str:
    """Drop reasoning tags. JSON inside them is not a command and cannot approve."""
    stripped = _THINK_BLOCK.sub("", text)
    match = _THINK_OPEN.search(stripped)
    if match:
        stripped = stripped[: match.start()]
    return stripped


def _strip_fence(text: str) -> str:
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _json_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    found: list[dict] = []
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        index = max(end, start + 1)
    return found


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
        if "synthetic project local-summary" in blob:
            if not any(item.get("role") == "tool" and item.get("name") == "storage.read" for item in messages):
                return ModelResponse(kind="tool", name="storage.read", arguments={"path": "notes.md"})
            if "LOCAL-SUMMARY-INPUT" not in blob:
                raise SsiosError("mock_missing_context", "mock context is incomplete", 500)
            text = "Synthetic local summary\n- LOCAL-SUMMARY-INPUT"
            return ModelResponse(kind="final", text=text, raw_approve_flag=True)
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
        if kind != "project-summary":
            raise SsiosError("task_rejected", "this task kind requires the mock provider", 400)
        if cancel_event.is_set() or time.monotonic() >= deadline:
            raise FlowInterrupt("timeout")
        remaining = deadline - time.monotonic()
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
            with urllib.request.urlopen(request, timeout=min(self.timeout, remaining)) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SsiosError("llm_unavailable", f"ollama http {exc.code}", 503) from exc
        except (TimeoutError, socket.timeout) as exc:
            if time.monotonic() >= deadline:
                raise FlowInterrupt("timeout") from exc
            raise SsiosError("llm_unavailable", "ollama timed out", 503) from exc
        except urllib.error.URLError as exc:
            raise SsiosError("llm_unavailable", "ollama is unreachable", 503) from exc
        except json.JSONDecodeError as exc:
            raise SsiosError("llm_unavailable", "ollama response is incomplete", 503) from exc
        if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
            raise SsiosError("llm_unavailable", "ollama response is incomplete", 503)
        content = body["message"].get("content", "")
        if not str(content).strip():
            raise SsiosError("model_output_invalid", "model output is not JSON", 502)
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

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

# Ollama applies this schema to message.content. message.thinking is never part of it.
TASK_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["final", "tool"]},
        "text": {"type": "string"},
        "name": {"type": "string", "enum": ["memory.read", "storage.read", "memory.search"]},
        "arguments": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "query": {"type": "string"},
            },
        },
    },
    "required": ["type"],
}

MARKERS = ("CORE-PROJECT-FACT", "THREE-BULLET-RULE", "NORTHWIND-FIXTURE-NOTE")


@dataclass
class ModelResponse:
    kind: str
    name: str = ""
    arguments: dict = field(default_factory=dict)
    text: str = ""
    raw_approve_flag: bool = False


def parse_model_payload(text: str) -> ModelResponse:
    """Validate one task object. Think tags are removed and are not commands."""
    visible = _strip_fence(_strip_think(text)).strip()
    if not visible:
        raise SsiosError("model_output_empty", "model returned no answer content", 502)
    try:
        obj = json.loads(visible)
    except json.JSONDecodeError as exc:
        if _looks_truncated(visible):
            raise SsiosError("model_output_truncated", "model output was truncated", 502) from exc
        raise SsiosError("model_output_invalid", "model output is not JSON", 502) from exc
    if not isinstance(obj, dict):
        raise SsiosError("model_output_invalid", "model output is not an object", 502)
    return _validate_task_object(obj)


def interpret_ollama_response(body: dict) -> ModelResponse:
    """Read a documented /api/chat body. Reasoning is never the command."""
    if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
        raise SsiosError("llm_response_unsupported", "ollama response is unsupported", 502)
    if body.get("done") is False or body.get("done_reason") == "length":
        raise SsiosError("model_output_truncated", "model output was truncated", 502)
    content = body["message"].get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise SsiosError("model_output_empty", "model returned no answer content", 502)
    return parse_model_payload(content)


def _validate_task_object(obj: dict) -> ModelResponse:
    kind = obj.get("type")
    approve = obj.get("approve") is True
    if kind == "final":
        text = obj.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SsiosError("model_output_invalid", "model output is missing text", 502)
        return ModelResponse(kind="final", text=text, raw_approve_flag=approve)
    if kind == "tool":
        name = obj.get("name")
        arguments = obj.get("arguments")
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise SsiosError("model_output_invalid", "tool arguments rejected", 502)
        cleaned: dict[str, str] = {}
        for key in ("path", "query"):
            if key not in arguments:
                continue
            if not isinstance(arguments[key], str):
                raise SsiosError("model_output_invalid", "tool arguments rejected", 502)
            cleaned[key] = arguments[key]
        return ModelResponse(kind="tool", name=name, arguments=cleaned, raw_approve_flag=approve)
    raise SsiosError("model_output_invalid", "model output type rejected", 502)


def _looks_truncated(text: str) -> bool:
    return text.count("{") > text.count("}") or text.count("[") > text.count("]")


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


def _chat_payload(model: str, messages: list[dict], fmt: dict | str) -> dict:
    grounded = []
    schema_note = "JSON schema for the answer content: " + json.dumps(TASK_OUTPUT_SCHEMA, separators=(",", ":"))
    for message in _to_ollama(messages):
        if not grounded and message.get("role") == "system":
            grounded.append({**message, "content": message["content"] + "\n" + schema_note})
        else:
            grounded.append(message)
    if not grounded:
        grounded.append({"role": "system", "content": schema_note})
    return {
        "model": model,
        "messages": grounded,
        "stream": False,
        "format": fmt,
        "options": {"temperature": 0},
    }


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        exc.read()
        if exc.code == 400:
            raise SsiosError("ollama_rejected", "ollama rejected the request", 400) from exc
        raise SsiosError("llm_unavailable", f"ollama http {exc.code}", 503) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise SsiosError("llm_unavailable", "ollama timed out", 503) from exc
    except urllib.error.URLError as exc:
        raise SsiosError("llm_unavailable", "ollama is unreachable", 503) from exc
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SsiosError("llm_response_unsupported", "ollama response is unsupported", 502) from exc
    if not isinstance(body, dict):
        raise SsiosError("llm_response_unsupported", "ollama response is unsupported", 502)
    return body


def probe_ollama(base_url: str, model: str, timeout: float) -> dict:
    """Bounded local capability check. Does not create a task or read memory."""
    base = base_url.rstrip("/")
    report: dict = {
        "model": model,
        "cloud_fallback": False,
        "ollama_version": None,
        "model_found": False,
        "thinking_values": [],
        "thinking_default": None,
        "capabilities": [],
        "format_accepted": None,
        "done_reason": None,
        "content_present": False,
        "thinking_present": False,
        "structured_output": False,
        "output_kind": None,
        "error": None,
        "detail": "",
    }
    try:
        version = _get_json(base + "/api/version", min(5.0, timeout))
    except SsiosError as exc:
        report["error"] = exc.code
        report["detail"] = exc.detail
        return report
    if isinstance(version, dict):
        value = version.get("version")
        report["ollama_version"] = value if isinstance(value, str) else None
    try:
        shown = _post_json(base + "/api/show", {"model": model}, min(10.0, timeout))
    except SsiosError as exc:
        report["error"] = "model_missing" if exc.detail == "ollama http 404" else exc.code
        report["detail"] = "configured model was not found" if report["error"] == "model_missing" else exc.detail
        return report
    report["model_found"] = True
    thinking = shown.get("thinking") if isinstance(shown.get("thinking"), dict) else {}
    values = thinking.get("values")
    report["thinking_values"] = [item for item in values if isinstance(item, (bool, str))] if isinstance(values, list) else []
    default = thinking.get("default")
    report["thinking_default"] = default if isinstance(default, (bool, str)) else None
    capabilities = shown.get("capabilities")
    if isinstance(capabilities, list):
        report["capabilities"] = [item for item in capabilities if isinstance(item, str) and len(item) <= 40][:12]
    chat_timeout = min(45.0, timeout)
    messages = [
        {"role": "system", "content": "Return one JSON object and no personal data."},
        {"role": "user", "content": 'Set type to final and text to probe-ok.'},
    ]
    payload = _chat_payload(model, messages, TASK_OUTPUT_SCHEMA)
    payload["options"] = {"temperature": 0, "num_predict": 128}
    try:
        body = _post_json(base + "/api/chat", payload, chat_timeout)
        report["format_accepted"] = "schema"
    except SsiosError as exc:
        if exc.code != "ollama_rejected":
            report["error"] = exc.code
            report["detail"] = exc.detail
            return report
        payload["format"] = "json"
        try:
            body = _post_json(base + "/api/chat", payload, chat_timeout)
        except SsiosError as second:
            report["error"] = "model_format_unsupported" if second.code == "ollama_rejected" else second.code
            report["detail"] = "ollama rejected structured format" if second.code == "ollama_rejected" else second.detail
            return report
        report["format_accepted"] = "json"
    message = body.get("message") if isinstance(body.get("message"), dict) else {}
    content = message.get("content")
    thinking_text = message.get("thinking")
    report["content_present"] = isinstance(content, str) and bool(content.strip())
    report["thinking_present"] = isinstance(thinking_text, str) and bool(thinking_text.strip())
    report["done_reason"] = body.get("done_reason") if isinstance(body.get("done_reason"), str) else None
    try:
        parsed = interpret_ollama_response(body)
    except SsiosError as exc:
        report["error"] = exc.code
        report["detail"] = exc.detail
        return report
    report["output_kind"] = parsed.kind
    report["structured_output"] = parsed.kind == "final" and bool(parsed.text.strip())
    report["detail"] = "structured content validated; thinking text was not used"
    return report


def _get_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        exc.read()
        raise SsiosError("llm_unavailable", f"ollama http {exc.code}", 503) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise SsiosError("llm_unavailable", "ollama timed out", 503) from exc
    except urllib.error.URLError as exc:
        raise SsiosError("llm_unavailable", "ollama is unreachable", 503) from exc
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SsiosError("llm_response_unsupported", "ollama response is unsupported", 502) from exc
    if not isinstance(body, dict):
        raise SsiosError("llm_response_unsupported", "ollama response is unsupported", 502)
    return body


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
        payload = _chat_payload(self.model, messages, TASK_OUTPUT_SCHEMA)
        timeout = min(self.timeout, remaining)
        try:
            body = _post_json(self.base_url + "/api/chat", payload, timeout)
        except SsiosError as exc:
            if exc.code == "llm_unavailable" and "timed out" in exc.detail and time.monotonic() >= deadline:
                raise FlowInterrupt("timeout") from exc
            if exc.code != "ollama_rejected":
                raise
            payload["format"] = "json"
            try:
                body = _post_json(self.base_url + "/api/chat", payload, timeout)
            except SsiosError as second:
                if second.code == "ollama_rejected":
                    raise SsiosError("model_format_unsupported", "ollama rejected structured format", 502) from second
                if second.code == "llm_unavailable" and "timed out" in second.detail and time.monotonic() >= deadline:
                    raise FlowInterrupt("timeout") from second
                raise
        return interpret_ollama_response(body)


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

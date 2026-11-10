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


def classify_model_text(text: str) -> str:
    """Privacy-safe label for a failed model answer. Does not decide the command."""
    if not isinstance(text, str) or not text.strip():
        return "empty"
    lowered = text.casefold()
    if "<think" in lowered:
        return "think_tags"
    if "```" in text:
        return "fenced_json"
    visible = _visible_answer(text)
    objects = _top_level_objects(visible)
    if len(objects) > 1:
        return "multiple_json"
    if len(objects) == 1 and visible.strip() != json.dumps(objects[0]):
        try:
            loaded = json.loads(visible)
        except json.JSONDecodeError:
            loaded = None
        if not isinstance(loaded, dict):
            try:
                _validate_task_object(objects[0])
            except SsiosError:
                return "schema_mismatch"
            return "prose_around_json"
    if _looks_truncated(visible):
        return "truncated"
    if objects:
        try:
            _validate_task_object(objects[0])
        except SsiosError:
            return "schema_mismatch"
        return "json"
    return "not_json"


def parse_model_payload(text: str) -> ModelResponse:
    """Validate one task object. Think tags and surrounding prose are not commands."""
    visible = _visible_answer(text)
    if not visible.strip():
        raise _invalid_output(text, "model_output_empty", "model returned no answer content")
    try:
        loaded = json.loads(visible)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        chosen = loaded
    else:
        objects = _top_level_objects(visible)
        if len(objects) > 1:
            raise _invalid_output(text, "model_output_invalid", "model output has more than one JSON object")
        if len(objects) == 1:
            chosen = objects[0]
        elif _looks_truncated(visible):
            raise _invalid_output(text, "model_output_truncated", "model output was truncated")
        else:
            raise _invalid_output(text, "model_output_invalid", "model output is not JSON")
    try:
        return _validate_task_object(chosen)
    except SsiosError as exc:
        exc.diagnostic = _diagnostic(text, "schema_mismatch")
        raise


def _invalid_output(text: str, code: str, detail: str) -> SsiosError:
    exc = SsiosError(code, detail, 502)
    exc.diagnostic = _diagnostic(text, classify_model_text(text))
    return exc


def _diagnostic(content: str, classification: str, thinking: str = "") -> dict:
    return {
        "classification": classification,
        "content": content if isinstance(content, str) else "",
        "thinking": thinking if isinstance(thinking, str) else "",
    }


def _visible_answer(text: str) -> str:
    return _strip_fences(_strip_think(text)).strip()


def _top_level_objects(text: str) -> list[dict]:
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
        else:
            index = start + 1
    return found


def interpret_ollama_response(body: dict) -> ModelResponse:
    """Read a documented /api/chat body. Reasoning is never the command."""
    if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
        raise SsiosError("llm_response_unsupported", "ollama response is unsupported", 502)
    if body.get("done") is False or body.get("done_reason") == "length":
        raise SsiosError("model_output_truncated", "model output was truncated", 502)
    message = body["message"]
    content = message.get("content", "")
    thinking = message.get("thinking", "")
    thinking_text = thinking if isinstance(thinking, str) else ""
    if not isinstance(content, str) or not content.strip():
        exc = SsiosError("model_output_empty", "model returned no answer content", 502)
        exc.diagnostic = _diagnostic("" if not isinstance(content, str) else content, "empty", thinking_text)
        raise exc
    try:
        return parse_model_payload(content)
    except SsiosError as exc:
        diagnostic = getattr(exc, "diagnostic", None)
        if isinstance(diagnostic, dict):
            diagnostic["thinking"] = thinking_text
            exc.diagnostic = diagnostic
        raise


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


_LOW_QUALITY_MARKERS = (
    "agent id",
    "agent_id",
    "agent-local-",
    "upstream token",
    "upstream_token",
    "token_estimate",
    "token total",
    "payload_sha256",
    "core_token_budget",
    "project id",
)


def validate_summary_quality(text: str) -> None:
    """Reject summaries that are too thin, truncated, or just an internal dump."""
    stripped = text.strip()
    if len(stripped) < 20:
        raise SsiosError("model_output_low_quality", "summary is too short", 502)
    if stripped.count("```") % 2 == 1 or _looks_truncated(stripped):
        raise SsiosError("model_output_low_quality", "summary is truncated", 502)
    if stripped.startswith("{") or stripped.startswith("["):
        raise SsiosError("model_output_low_quality", "summary is raw JSON", 502)
    folded = stripped.casefold()
    if any(marker in folded for marker in _LOW_QUALITY_MARKERS):
        raise SsiosError("model_output_low_quality", "summary echoes internal metadata", 502)
    if any(phrase in folded for phrase in _TEMPLATE_ECHOES):
        raise SsiosError("model_output_low_quality", "summary restates template instructions", 502)


_TEMPLATE_ECHOES = (
    "learn more about the person",
    "person i'm working with",
    "person i’m working with",
    "i am a helpful assistant",
)


def is_template_boilerplate(path: str, content: str) -> bool:
    """True when a core template file has no meaningful user prose."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if name not in {"human.md", "persona.md"}:
        return False
    folded = content.casefold()
    leftover = folded
    for phrase in _TEMPLATE_ECHOES:
        leftover = leftover.replace(phrase, " ")
    leftover = re.sub(r"[^a-z0-9]+", " ", leftover).strip()
    return len(leftover) < 80


def think_false_supported(shown: dict) -> bool:
    """True when /api/show says the model can be asked not to spend tokens on reasoning."""
    if not isinstance(shown, dict):
        return False
    if isinstance(shown.get("message"), dict) and "capabilities" not in shown and "thinking" not in shown:
        return False
    capabilities = shown.get("capabilities")
    if isinstance(capabilities, list) and "thinking" in capabilities:
        return True
    thinking = shown.get("thinking")
    if not isinstance(thinking, dict):
        return False
    values = thinking.get("values")
    return isinstance(values, list) and (False in values or "false" in values)


def _strip_think(text: str) -> str:
    """Drop reasoning tags. JSON inside them is not a command and cannot approve."""
    stripped = _THINK_BLOCK.sub("", text)
    match = _THINK_OPEN.search(stripped)
    if match:
        stripped = stripped[: match.start()]
    return stripped


def _strip_fences(text: str) -> str:
    return re.sub(r"```(?:json|JSON)?[^\n]*\n(.*?)```", r"\1", text, flags=re.DOTALL)


def _strip_fence(text: str) -> str:
    return _strip_fences(text).strip()


def _chat_payload(
    model: str,
    messages: list[dict],
    fmt: dict | str | None,
    *,
    num_predict: int,
    think: bool | None,
    num_ctx: int = 8192,
    schema_hint: bool = True,
) -> dict:
    grounded = []
    schema_note = "JSON schema for the answer content: " + json.dumps(TASK_OUTPUT_SCHEMA, separators=(",", ":"))
    for message in _to_ollama(messages):
        content = message["content"]
        if schema_hint and not grounded and message.get("role") == "system":
            content = content + "\n" + schema_note
        if not grounded and message.get("role") == "system":
            grounded.append({**message, "content": content})
        else:
            grounded.append(message)
    if not grounded:
        grounded.append({"role": "system", "content": schema_note})
    payload = {
        "model": model,
        "messages": grounded,
        "stream": False,
        "options": {
            "temperature": 0,
            "num_predict": num_predict,
            "num_ctx": num_ctx,
            "repeat_penalty": 1.15,
        },
    }
    if fmt is not None:
        payload["format"] = fmt
    if think is False:
        payload["think"] = False
    return payload


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


def _needs_budget_retry(body: dict) -> bool:
    if body.get("done") is False or body.get("done_reason") == "length":
        return True
    message = body.get("message") if isinstance(body.get("message"), dict) else {}
    content = message.get("content")
    thinking = message.get("thinking")
    return (not isinstance(content, str) or not content.strip()) and isinstance(thinking, str) and bool(thinking.strip())


def _chat_with_fallback(url: str, payload: dict, timeout: float) -> dict:
    """Schema first, then json. Drop think=false if the server rejects it. At most those retries."""
    try:
        return _post_json(url, payload, timeout)
    except SsiosError as exc:
        if exc.code != "ollama_rejected":
            raise
    if payload.get("think") is False:
        payload.pop("think", None)
        try:
            return _post_json(url, payload, timeout)
        except SsiosError as exc:
            if exc.code != "ollama_rejected":
                raise
    if isinstance(payload.get("format"), dict):
        payload["format"] = "json"
        try:
            return _post_json(url, payload, timeout)
        except SsiosError as exc:
            if exc.code == "ollama_rejected":
                raise SsiosError("model_format_unsupported", "ollama rejected structured format", 502) from exc
            raise
    raise SsiosError("model_format_unsupported", "ollama rejected structured format", 502)


def probe_ollama(base_url: str, model: str, timeout: float, *, max_output_tokens: int = 512) -> dict:
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
        "think_requested": None,
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
    think = False if think_false_supported(shown) else None
    report["think_requested"] = think
    messages = [
        {"role": "system", "content": "Return one JSON object and no personal data."},
        {"role": "user", "content": "Set type to final and text to probe-ok."},
    ]
    budget = max(64, min(int(max_output_tokens), 8192))
    payload = _chat_payload(model, messages, TASK_OUTPUT_SCHEMA, num_predict=budget, think=think)
    try:
        body = _chat_with_fallback(base + "/api/chat", payload, chat_timeout)
        report["format_accepted"] = "schema" if isinstance(payload.get("format"), dict) else "json"
        report["think_requested"] = False if payload.get("think") is False else None
    except SsiosError as exc:
        report["error"] = exc.code
        report["detail"] = exc.detail
        return report
    if _needs_budget_retry(body):
        payload["options"]["num_predict"] = min(budget * 2, 8192)
        try:
            body = _chat_with_fallback(base + "/api/chat", payload, chat_timeout)
        except SsiosError as exc:
            report["error"] = exc.code
            report["detail"] = exc.detail
            return report
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
        if "HARBOR-LOCK" in blob and "Northwind Lantern" in blob:
            text = (
                "HARBOR-LOCK keeps approvals with the human and refuses a cloud fallback. "
                "The Northwind Lantern meeting kept project memory on the laptop. "
                "The reading note says MemFS is git markdown, not a vector index."
            )
            return ModelResponse(kind="final", text=text, raw_approve_flag=True)
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

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float,
        max_output_tokens: int = 1024,
        num_ctx: int = 8192,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.num_ctx = num_ctx
        self._think_false: bool | None = None

    def wants_plain_summary(self, deadline: float) -> bool:
        """Thinking models summarize in prose. JSON is retried only for other models."""
        return self._supports_think_false(deadline)

    def complete(self, messages: list[dict], *, kind: str, cancel_event, deadline: float) -> ModelResponse:
        if kind != "project-summary":
            raise SsiosError("task_rejected", "this task kind requires the mock provider", 400)
        if cancel_event.is_set() or time.monotonic() >= deadline:
            raise FlowInterrupt("timeout")
        think = False if self._supports_think_false(deadline) else None
        body = self._ask(messages, think, self.max_output_tokens, deadline)
        if _needs_budget_retry(body):
            body = self._ask(messages, False if self._think_false else None, min(self.max_output_tokens * 2, 8192), deadline)
        return interpret_ollama_response(body)

    def _supports_think_false(self, deadline: float) -> bool:
        if self._think_false is not None:
            return self._think_false
        timeout = min(10.0, self.timeout, max(0.1, deadline - time.monotonic()))
        try:
            shown = _post_json(self.base_url + "/api/show", {"model": self.model}, timeout)
        except SsiosError:
            self._think_false = False
            return False
        self._think_false = think_false_supported(shown)
        return self._think_false

    def _ask(self, messages: list[dict], think: bool | None, num_predict: int, deadline: float) -> dict:
        timeout = min(self.timeout, max(0.1, deadline - time.monotonic()))
        payload = _chat_payload(
            self.model,
            messages,
            TASK_OUTPUT_SCHEMA,
            num_predict=num_predict,
            think=think,
            num_ctx=self.num_ctx,
        )
        try:
            body = _chat_with_fallback(self.base_url + "/api/chat", payload, timeout)
        except SsiosError as exc:
            if exc.code == "llm_unavailable" and "timed out" in exc.detail and time.monotonic() >= deadline:
                raise FlowInterrupt("timeout") from exc
            raise
        if think is False and payload.get("think") is not False:
            self._think_false = False
        return body

    def complete_prose(self, messages: list[dict], *, kind: str, cancel_event, deadline: float) -> str:
        """Summary prose. A schema-valid final object is accepted; thinking text is not."""
        if kind != "project-summary":
            raise SsiosError("task_rejected", "this task kind requires the mock provider", 400)
        if cancel_event.is_set() or time.monotonic() >= deadline:
            raise FlowInterrupt("timeout")
        think = False if self._supports_think_false(deadline) else None
        body = self._ask_prose(messages, think, self.max_output_tokens, deadline)
        if _needs_budget_retry(body):
            body = self._ask_prose(messages, False if self._think_false else None, min(self.max_output_tokens * 2, 8192), deadline)
        if body.get("done") is False or body.get("done_reason") == "length":
            raise SsiosError("model_output_truncated", "model output was truncated", 502)
        message = body.get("message") if isinstance(body.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), str) else ""
        thinking = message.get("thinking") if isinstance(message.get("thinking"), str) else ""
        visible = _visible_answer(content)
        try:
            parsed = parse_model_payload(content)
        except SsiosError as exc:
            if exc.code == "model_output_truncated":
                raise
            if visible.strip() and not visible.strip().startswith("{") and not visible.strip().startswith("["):
                return visible.strip()
            diagnostic = getattr(exc, "diagnostic", None)
            if isinstance(diagnostic, dict):
                diagnostic["thinking"] = thinking
                exc.diagnostic = diagnostic
            raise
        if parsed.kind == "final":
            return parsed.text
        exc = SsiosError("model_output_invalid", "model output is not a summary", 502)
        exc.diagnostic = _diagnostic(content, "schema_mismatch", thinking)
        raise exc

    def _ask_prose(self, messages: list[dict], think: bool | None, num_predict: int, deadline: float) -> dict:
        timeout = min(self.timeout, max(0.1, deadline - time.monotonic()))
        payload = _chat_payload(
            self.model,
            messages,
            None,
            num_predict=num_predict,
            think=think,
            num_ctx=self.num_ctx,
            schema_hint=False,
        )
        try:
            body = _post_json(self.base_url + "/api/chat", payload, timeout)
        except SsiosError as exc:
            if exc.code == "ollama_rejected" and payload.get("think") is False:
                payload.pop("think", None)
                self._think_false = False
                body = _post_json(self.base_url + "/api/chat", payload, timeout)
            elif exc.code == "llm_unavailable" and "timed out" in exc.detail and time.monotonic() >= deadline:
                raise FlowInterrupt("timeout") from exc
            else:
                raise
        return body


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
        return OllamaLLM(
            settings.ollama_base_url,
            settings.ollama_model,
            settings.llm_timeout_seconds,
            settings.max_output_tokens,
            settings.num_ctx,
        )
    raise SsiosError("config_invalid", "llm provider rejected", 500)

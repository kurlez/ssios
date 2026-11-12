"""Local tool-choice bench. It talks only to loopback Ollama and writes nothing.

Each turn is one request. Synthetic prompts only. Tool results are not fetched,
and a write is not turned into an approval.
"""

from __future__ import annotations

import base64
import json
import statistics
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from ssios.errors import SsiosError
from ssios.mcp_host import WRITE_TOOLS, normalize_tool_name, text_only_tool_calls

MACHINE_BYTES = 24 * 1024**3
GUEST_BYTES = 2 * 1024**3
FREE_FLOOR = 3 * 1024**3
_SYNTHETIC_PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415408d763f8ffff3f0005fe02fe33d32f0000000049454e44ae426082"
    )
).decode("ascii")

TURNS = (
    {"id": "gmail-search", "user": "Search the inbox for mail from Harbor Desk.", "tool": "gmail.search", "required": ("q",), "write": False},
    {"id": "gmail-read", "user": "Read the synthetic message msg-1.", "tool": "gmail.read", "required": ("id",), "write": False},
    {"id": "calendar-list", "user": "List calendar events for this week.", "tool": "calendar.list", "required": (), "write": False},
    {"id": "drive-list", "user": "Search Drive for the studio plan.", "tool": "drive.list", "required": ("q",), "write": False},
    {"id": "weather", "user": "What is the weather in Seattle?", "tool": "weather", "required": ("place",), "write": False},
    {"id": "web-fetch", "user": "Fetch https://example.com/harbor", "tool": "web.fetch", "required": ("url",), "write": False},
    {"id": "gmail-unread", "user": "Search unread inbox mail.", "tool": "gmail.search", "required": ("q",), "write": False},
    {"id": "drive-read", "user": "Read Drive file id file-1.", "tool": "drive.read", "required": ("id",), "write": False},
    {"id": "task-write", "user": "Add a task titled Call the harbor desk.", "tool": "tasks.add", "required": ("title",), "write": True},
    {"id": "calendar-tomorrow", "user": "List tomorrow's calendar events.", "tool": "calendar.list", "required": (), "write": False},
)


def tool_schemas() -> list[dict]:
    """The same names the guest is told to call. Dots, not underscores."""
    specs = {
        "gmail.search": {"q": {"type": "string"}},
        "gmail.read": {"id": {"type": "string"}},
        "calendar.list": {},
        "drive.list": {"q": {"type": "string"}},
        "drive.read": {"id": {"type": "string"}},
        "weather": {"place": {"type": "string"}},
        "web.fetch": {"url": {"type": "string"}},
        "tasks.add": {"title": {"type": "string"}},
    }
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": props},
            },
        }
        for name, props in specs.items()
    ]


def run_bench(*, model: str, base_url: str, opener=None, num_ctx: int = 16384, vision: bool = False, vision_model: str = "qwen2.5vl:7b") -> dict:
    """Score one local model. opener(url, payload) returns (status, body). payload None is a GET."""
    host = (urlsplit(base_url).hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SsiosError("network_denied", "model-bench only talks to loopback Ollama.", 403)
    if not model or any(char in model for char in "\n\r\"/"):
        raise SsiosError("config_invalid", "model name is rejected", 400)
    post = opener or _loopback_open
    tools = tool_schemas()
    rows = []
    times = []
    for turn in TURNS:
        started = time.monotonic()
        status, raw = post(
            base_url.rstrip("/") + "/api/chat",
            {
                "model": model,
                "stream": False,
                "think": False,
                "tools": tools,
                "messages": [
                    {"role": "system", "content": "Call one tool. Use dotted names such as gmail.search. Do not invent mail."},
                    {"role": "user", "content": turn["user"]},
                ],
                "options": {"num_ctx": int(num_ctx), "num_predict": 256},
            },
        )
        elapsed = time.monotonic() - started
        times.append(elapsed)
        rows.append(_score(turn, status, raw, elapsed))
    memory = _memory(post, base_url)
    counted = [row for row in rows if row["http_status"] == 200]
    total = len(TURNS)
    report = {
        "model": model,
        "turns": total,
        "valid_tool_call_rate": _rate(sum(row["valid"] for row in counted), total),
        "correct_tool_rate": _rate(sum(row["correct"] for row in counted), total),
        "argument_validity_rate": _rate(sum(row["args_valid"] for row in counted), total),
        "tool_call_as_text_count": sum(row["as_text"] for row in rows),
        "median_seconds": round(statistics.median(times), 3) if times else 0,
        "max_seconds": round(max(times), 3) if times else 0,
        "write_needs_approval": all(row["write_needs_approval"] for row in rows if row["expect_write"]),
        "peak_memory": memory,
        "warning": memory.get("warning") or "",
        "synthetic": True,
        "rows": rows,
    }
    if vision:
        report["vision"] = _vision_probe(post, base_url, vision_model)
    return report


def _vision_probe(post, base_url: str, model: str) -> dict:
    """One synthetic image. Nothing is saved. A missing model names the pull command."""
    try:
        status, raw = post(
            base_url.rstrip("/") + "/api/chat",
            {
                "model": model,
                "stream": False,
                "messages": [
                    {
                        "role": "user",
                        "content": "Read the text in this synthetic image. Reply with one word.",
                        "images": [_SYNTHETIC_PNG],
                    }
                ],
            },
        )
    except SsiosError:
        return {"ok": False, "installed": False, "model": model, "detail": f"The vision model {model} is not installed. Run: ollama pull {model}"}
    if status == 404:
        return {"ok": False, "installed": False, "model": model, "detail": f"The vision model {model} is not installed. Run: ollama pull {model}"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        payload = {}
    message = payload.get("message") if isinstance(payload, dict) else {}
    caption = str(message.get("content") or "").strip() if isinstance(message, dict) else ""
    return {"ok": bool(caption), "installed": status == 200, "model": model, "caption": caption[:200]}


def _score(turn: dict, status: int, raw: bytes, elapsed: float) -> dict:
    native, text_calls = _calls(raw)
    chosen = native or text_calls
    name = ""
    arguments: dict = {}
    if chosen:
        name = normalize_tool_name(str(chosen[0].get("name") or ""))
        arguments = chosen[0].get("arguments") if isinstance(chosen[0].get("arguments"), dict) else {}
    valid = bool(name) and isinstance(arguments, dict)
    required = tuple(turn["required"])
    args_valid = valid and all(str(arguments.get(key) or "").strip() for key in required)
    correct = name == turn["tool"]
    expect_write = bool(turn["write"])
    return {
        "id": turn["id"],
        "http_status": int(status),
        "valid": valid,
        "correct": correct and valid,
        "args_valid": args_valid and correct,
        "as_text": 1 if text_calls and not native else 0,
        "tool": name,
        "expect_write": expect_write,
        "write_needs_approval": (not expect_write) or (name in WRITE_TOOLS),
        "seconds": round(elapsed, 3),
    }


def _calls(raw: bytes) -> tuple[list[dict], list[dict]]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return [], []
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        return [], []
    native = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else call
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if isinstance(arguments, dict):
            native.append({"name": function.get("name") or "", "arguments": arguments})
    content = message.get("content") if isinstance(message.get("content"), str) else ""
    return native, text_only_tool_calls(content) or []


def _memory(post, base_url: str) -> dict:
    try:
        status, raw = post(base_url.rstrip("/") + "/api/ps", None)
    except SsiosError:
        return {"size": None, "size_vram": None, "peak_bytes": None, "warning": "Ollama did not report /api/ps."}
    if status != 200:
        return {"size": None, "size_vram": None, "peak_bytes": None, "warning": "Ollama did not report /api/ps."}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return {"size": None, "size_vram": None, "peak_bytes": None, "warning": "Ollama did not report /api/ps."}
    size = 0
    vram = 0
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        size = max(size, int(item.get("size") or 0))
        vram = max(vram, int(item.get("size_vram") or 0))
    peak = max(size, vram)
    free = MACHINE_BYTES - peak - GUEST_BYTES
    warning = ""
    if peak and free < FREE_FLOOR:
        warning = "The model plus a 2GB guest leaves under 3GB free on a 24GB machine. Prefer num_ctx 16384, or stay on qwen3:14b."
    return {"size": size, "size_vram": vram, "peak_bytes": peak, "free_after_guest_bytes": free, "warning": warning}


def _rate(count: int, total: int) -> float:
    if not total:
        return 0.0
    return round(count / total, 3)


def _loopback_open(url: str, payload) -> tuple[int, bytes]:
    host = (urlsplit(url).hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SsiosError("network_denied", "model-bench only talks to loopback Ollama.", 403)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="GET" if payload is None else "POST")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except urllib.error.URLError as exc:
        raise SsiosError("llm_unavailable", "Ollama is not reachable on loopback.", 503) from exc

"""Score the router on synthetic prompts. Rules-only does not open a socket."""

from __future__ import annotations

import json
import statistics
import time
import urllib.request
from urllib.parse import urlsplit

from ssios.chat_router import classify
from ssios.errors import SsiosError
from ssios.route_classifier import CLASSIFIER_SCHEMA, CLASSIFIER_SYSTEM, decision_from_classifier, parse_classifier
from ssios.router_cases import CASES

CLASSIFIER_MODELS = ("qwen3:1.7b", "qwen3:4b")


def score_rules() -> dict:
    """Rules only. This is what CI runs."""
    rows = []
    times = []
    for case in CASES:
        started = time.perf_counter()
        decision = classify(case["prompt"])
        elapsed = (time.perf_counter() - started) * 1000
        times.append(elapsed)
        rows.append(_row(case, decision.route, _kind(decision), elapsed, "rules"))
    return _report("rules", rows, times)


def score_model(model: str, *, base_url: str = "http://127.0.0.1:11434", opener=None, timeout: float = 4.0) -> dict:
    """Rules first, then one classifier call when the rules are not the whole story.

    The opener is for tests. The default opener talks only to loopback Ollama.
    """
    host = (urlsplit(base_url).hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SsiosError("network_denied", "router-bench only talks to loopback Ollama.", 403)
    if not model or any(char in model for char in "\n\r\"/"):
        raise SsiosError("config_invalid", "model name is rejected", 400)
    post = opener or _loopback_open
    rows = []
    times = []
    for case in CASES:
        started = time.perf_counter()
        route, kind = _ask_model(post, base_url, model, case["prompt"], timeout)
        elapsed = (time.perf_counter() - started) * 1000
        times.append(elapsed)
        rows.append(_row(case, route, kind, elapsed, model))
    return _report(model, rows, times)


def _ask_model(post, base_url: str, model: str, prompt: str, timeout: float) -> tuple[str, str]:
    try:
        status, raw = post(
            base_url.rstrip("/") + "/api/chat",
            {
                "model": model,
                "stream": False,
                "think": False,
                "format": CLASSIFIER_SCHEMA,
                "messages": [
                    {"role": "system", "content": CLASSIFIER_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "options": {"temperature": 0, "num_predict": 80, "num_ctx": 4096},
            },
            timeout,
        )
    except (OSError, TimeoutError):
        return "direct", ""
    if status != 200:
        return "direct", ""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return "direct", ""
    message = body.get("message") if isinstance(body.get("message"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), str) else ""
    chosen = decision_from_classifier(parse_classifier(content))
    if chosen is None:
        return "direct", ""
    return chosen.route, _kind(chosen)


def _kind(decision) -> str:
    if decision.route == "skill":
        return str(decision.skill or "")
    if decision.services:
        return str(decision.services[0])
    return ""


def _row(case: dict, route: str, kind: str, elapsed: float, source: str) -> dict:
    expected = case["route"]
    ok = route == expected and (not case["kind"] or kind == case["kind"])
    return {
        "id": case["id"],
        "expected": expected,
        "predicted": route,
        "ok": ok,
        "latency_ms": round(elapsed, 3),
        "source": source,
    }


def _report(name: str, rows: list[dict], times: list[float]) -> dict:
    correct = sum(1 for row in rows if row["ok"])
    confusion: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = confusion.setdefault(row["expected"], {})
        bucket[row["predicted"]] = bucket.get(row["predicted"], 0) + 1
    ordered = sorted(times)
    return {
        "mode": name,
        "count": len(rows),
        "correct": correct,
        "accuracy": round(correct / len(rows), 4) if rows else 0,
        "confusion": confusion,
        "latency_ms": {
            "median": round(statistics.median(ordered), 3) if ordered else 0,
            "max": round(ordered[-1], 3) if ordered else 0,
        },
        "rows": rows,
    }


def _loopback_open(url: str, payload: dict, timeout: float) -> tuple[int, bytes]:
    host = (urlsplit(url).hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SsiosError("network_denied", "router-bench only talks to loopback Ollama.", 403)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()

"""Compare small local embedding models. This does not download one and does not call a cloud host."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ssios.library_analyze import _loopback

CANDIDATES = ("bge-m3", "nomic-embed-text", "all-minilm", "mxbai-embed-large")
COMMAND = "./scripts/ssios library-embed-bench"


def embed_bench(base_url: str, timeout: float = 3) -> dict:
    """Probe loopback Ollama. A missing model is a skip, not a score."""
    report = {"skipped": False, "reason": "", "command": COMMAND, "results": []}
    if not _loopback(str(base_url or "")):
        report["skipped"] = True
        report["reason"] = "not-loopback"
        return report
    try:
        with urllib.request.urlopen(str(base_url).rstrip("/") + "/api/tags", timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        report["skipped"] = True
        report["reason"] = "ollama-unreachable"
        report["results"] = [{"model": name, "skipped": True, "reason": "not-tried"} for name in CANDIDATES]
        return report
    installed = set()
    for row in body.get("models") or []:
        if isinstance(row, dict) and row.get("name"):
            installed.add(str(row["name"]))
    any_ran = False
    for name in CANDIDATES:
        if name not in installed and not any(item == name or item.startswith(name + ":") for item in installed):
            report["results"].append({"model": name, "skipped": True, "reason": "not-installed"})
            continue
        started = time.perf_counter()
        try:
            payload = json.dumps({"model": name, "input": "red square"}).encode("utf-8")
            request = urllib.request.Request(
                str(base_url).rstrip("/") + "/api/embed",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                embedded = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            report["results"].append({"model": name, "skipped": True, "reason": "embed-failed"})
            continue
        vector = embedded.get("embeddings") if isinstance(embedded, dict) else None
        if not vector and isinstance(embedded, dict):
            vector = embedded.get("embedding")
        width = len(vector[0]) if isinstance(vector, list) and vector and isinstance(vector[0], list) else len(vector) if isinstance(vector, list) else 0
        if width <= 0:
            report["results"].append({"model": name, "skipped": True, "reason": "empty"})
            continue
        any_ran = True
        report["results"].append({"model": name, "skipped": False, "dimensions": width, "elapsed_ms": int((time.perf_counter() - started) * 1000)})
    if not any_ran:
        report["skipped"] = True
        report["reason"] = "no-candidate-installed"
    return report

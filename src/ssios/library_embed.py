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
        row = {"model": name, "skipped": False, "dimensions": width, "elapsed_ms": int((time.perf_counter() - started) * 1000)}
        try:
            from ssios.semantic import OllamaEmbedder

            row["quality"] = retrieval_quality(OllamaEmbedder(str(base_url), name, timeout=max(timeout, 30)))
        except Exception:
            row["quality"] = {"skipped": True, "reason": "embed-failed"}
        report["results"].append(row)
    if not any_ran:
        report["skipped"] = True
        report["reason"] = "no-candidate-installed"
    return report


# Synthetic text. Each query is in the other language from the document it should find.
QUALITY_DOCS = (
    "a sailing boat tied up at the harbor pier",
    "三月份的电费账单和发票",
    "notes from the weekly team meeting about the budget",
    "一只橘色的猫睡在沙发上",
    "a recipe for tomato and egg noodles",
    "护照和签证的扫描件",
)
QUALITY_QUERIES = ("港口的帆船", "electricity bill", "预算会议纪要", "cat photo", "番茄鸡蛋面的做法", "passport scan")


def retrieval_quality(embed) -> dict:
    """Six cross-language lookups. suggested_min_score sits between the worst right answer and the best wrong one."""
    import math

    def unit(vector):
        norm = math.sqrt(sum(float(item) * float(item) for item in vector)) or 1.0
        return [float(item) / norm for item in vector]

    docs = [unit(item) for item in embed(list(QUALITY_DOCS))]
    queries = [unit(item) for item in embed(list(QUALITY_QUERIES))]
    right = []
    wrong = []
    hits = 0
    for index, query in enumerate(queries):
        scores = [sum(a * b for a, b in zip(query, doc)) for doc in docs]
        if scores.index(max(scores)) == index:
            hits += 1
        right.append(scores[index])
        wrong.append(max(score for other, score in enumerate(scores) if other != index))
    lowest, highest = min(right), max(wrong)
    report = {"top1": f"{hits}/{len(queries)}", "lowest_right": round(lowest, 3), "highest_wrong": round(highest, 3)}
    if lowest > highest:
        report["suggested_min_score"] = round((lowest + highest) / 2, 2)
    return report

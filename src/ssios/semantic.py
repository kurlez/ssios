"""Search by meaning with a local embedding model.

Off until `[search] embed_model` names a model that loopback Ollama serves.
The index holds vectors and content hashes, not text. It is rebuilt from the
documents the caller may read right now, so a file that was forgotten, trashed,
or unshared leaves the index on the next query. A failure of the model is not
an error for the caller. The keyword result stands on its own.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.request
from array import array
from operator import mul
from pathlib import Path
from urllib.parse import urlparse

from ssios.errors import SsiosError

MAX_CHARS = 2000
BATCH = 16
MAX_DOCS = 5000


def loopback(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").casefold()
    return parsed.scheme in {"http", "https"} and host in {"127.0.0.1", "localhost", "::1"}


class OllamaEmbedder:
    """POST /api/embed on this computer. A host that is not loopback is refused."""

    def __init__(self, base_url: str, model: str, timeout: float = 20.0) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.model = str(model or "")
        self.timeout = float(timeout)

    def __call__(self, texts: list[str]) -> list[list[float]]:
        if not loopback(self.base_url):
            raise SsiosError("embed_unavailable", "The embedding host is not on this computer.", 400)
        payload = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError, OSError) as exc:
            raise SsiosError("embed_unavailable", "The embedding model did not answer.", 503) from exc
        vectors = body.get("embeddings") if isinstance(body, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise SsiosError("embed_unavailable", "The embedding reply is unsupported.", 502)
        return vectors


def embedder_for(plane):
    """The plane's embedder, or None when meaning search is off."""
    injected = getattr(plane, "embedder", None)
    if callable(injected):
        return injected
    settings = getattr(plane, "settings", None)
    model = str(getattr(settings, "embed_model", "") or "")
    if not model or str(getattr(settings, "llm_provider", "")) != "ollama":
        return None
    base = str(getattr(settings, "ollama_base_url", "") or "")
    if not loopback(base):
        return None
    made = OllamaEmbedder(base, model)
    plane.embedder = made
    return made


def _model_label(plane, embedder) -> str:
    name = str(getattr(embedder, "model", "") or getattr(getattr(plane, "settings", None), "embed_model", "") or "")
    return name or "injected"


def _unit(vector) -> array | None:
    try:
        values = array("f", (float(item) for item in vector))
    except (TypeError, ValueError):
        return None
    norm = math.sqrt(sum(map(mul, values, values)))
    if not values or not math.isfinite(norm) or norm <= 0:
        return None
    return array("f", (item / norm for item in values))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SemanticIndex:
    def __init__(self, path: Path, embedder, model: str) -> None:
        self.path = Path(path)
        self.embedder = embedder
        self.model = str(model)

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        if fresh:
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors ("
            "space TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, model TEXT NOT NULL, vec BLOB NOT NULL, "
            "PRIMARY KEY (space, key))"
        )
        return conn

    def rank(self, space: str, docs: dict[str, str], query: str, *, top_k: int = 8, min_score: float = 0.5, budget_seconds: float = 8.0) -> list[tuple[str, float]]:
        """Keys whose text is closest to the query, best first. Empty when the model cannot answer."""
        text = " ".join(str(query or "").split())[:MAX_CHARS]
        wanted = {str(key): str(body or "")[:MAX_CHARS] for key, body in list(docs.items())[:MAX_DOCS] if str(body or "").strip()}
        if not text:
            return []
        deadline = time.monotonic() + float(budget_seconds)
        try:
            conn = self._open()
        except (OSError, sqlite3.Error):
            return []
        try:
            vectors = self._sync(conn, space, wanted, deadline)
            if not vectors:
                return []
            asked = self.embedder([text])
            needle = _unit(asked[0]) if asked else None
        except (SsiosError, sqlite3.Error, OSError):
            return []
        finally:
            conn.close()
        if needle is None:
            return []
        scored = []
        for key, vector in vectors.items():
            if len(vector) != len(needle):
                continue
            score = sum(map(mul, needle, vector))
            if score >= min_score:
                scored.append((key, float(score)))
        scored.sort(key=lambda row: row[1], reverse=True)
        return scored[: max(1, int(top_k))]

    def _sync(self, conn: sqlite3.Connection, space: str, wanted: dict[str, str], deadline: float) -> dict[str, array]:
        stored = {}
        for key, digest, model, blob in conn.execute("SELECT key, digest, model, vec FROM vectors WHERE space = ?", (space,)):
            stored[key] = (digest, model, blob)
        stale = [key for key in stored if key not in wanted]
        if stale:
            conn.executemany("DELETE FROM vectors WHERE space = ? AND key = ?", [(space, key) for key in stale])
            conn.commit()
        vectors: dict[str, array] = {}
        missing = []
        for key, body in wanted.items():
            row = stored.get(key)
            if row and row[0] == _digest(body) and row[1] == self.model:
                vector = array("f")
                vector.frombytes(row[2])
                vectors[key] = vector
            else:
                missing.append(key)
        for start in range(0, len(missing), BATCH):
            if time.monotonic() >= deadline:
                break
            keys = missing[start : start + BATCH]
            made = self.embedder([wanted[key] for key in keys])
            rows = []
            for key, raw in zip(keys, made):
                vector = _unit(raw)
                if vector is None:
                    continue
                vectors[key] = vector
                rows.append((space, key, _digest(wanted[key]), self.model, vector.tobytes()))
            conn.executemany("INSERT OR REPLACE INTO vectors (space, key, digest, model, vec) VALUES (?, ?, ?, ?, ?)", rows)
            conn.commit()
        return vectors


def index_at(plane, path: Path) -> SemanticIndex | None:
    embedder = embedder_for(plane)
    if embedder is None:
        return None
    return SemanticIndex(path, embedder, _model_label(plane, embedder))


def _limits(plane) -> tuple[int, float]:
    settings = getattr(plane, "settings", None)
    return int(getattr(settings, "semantic_top_k", 8) or 8), float(getattr(settings, "semantic_min_score", 0.5) or 0.5)


def memory_hits(scope, query: str, *, prefix: str = "") -> list[str]:
    """Trusted memory files closest in meaning to the query. Empty when meaning search is off."""
    index = index_at(scope.plane, scope.home / "index" / "memory.sqlite3")
    if index is None:
        return []
    store = scope.memory()
    project = scope.project_id()
    docs = {}
    for rel in store.list_markdown(project):
        if prefix and not rel.startswith(prefix):
            continue
        try:
            doc = store.read_file(project, rel, include_untrusted=False)
        except SsiosError:
            continue
        if doc.get("tombstone"):
            continue
        docs[rel] = str(doc.get("content") or "")
    top_k, min_score = _limits(scope.plane)
    return [key for key, _score in index.rank("memory", docs, query, top_k=top_k, min_score=min_score)]


def library_hits(plane, root: Path, library_id: str, items: list[dict], query: str) -> list[str]:
    """Item ids closest in meaning to the query. The index sits beside the library it describes."""
    index = index_at(plane, Path(root) / "index.sqlite3")
    if index is None:
        return []
    from ssios.library_query import _blob

    docs = {str(item.get("id") or ""): _blob(item) for item in items if item.get("id")}
    top_k, min_score = _limits(plane)
    return [key for key, _score in index.rank("library:" + library_id, docs, query, top_k=top_k, min_score=min_score)]

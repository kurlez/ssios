"""Loopback JSON client used by the CLI."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def request(base: str, method: str, path: str, *, token: str | None = None, body: dict | None = None, headers: dict | None = None, timeout: float = 10):
    data = None if body is None else json.dumps(body).encode("utf-8")
    merged = {}
    if token:
        merged["Authorization"] = f"Bearer {token}"
    if body is not None:
        merged["Content-Type"] = "application/json"
    if headers:
        merged.update(headers)
    req = urllib.request.Request(base + path, data=data, headers=merged, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return response.status, parsed, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"error": "bad_response", "detail": raw[:200]}
        return exc.code, parsed, dict(exc.headers)

"""Summary worker that runs inside the guest.

The guest reads /input/grant.json and writes /scratch/request.json. The host
performs the model call and writes /scratch/response.json. The guest then
writes /scratch/output.json and exits. This file uses the standard library
only. It does not open a socket and it does not read a token.
"""

from __future__ import annotations

import json
import os
import time


def main() -> int:
    grant_path = os.environ.get("SSIOS_INPUT", "/input/grant.json")
    scratch = os.environ.get("SSIOS_SCRATCH", "/scratch")
    wait_seconds = float(os.environ.get("SSIOS_WAIT_SECONDS", "60"))
    with open(grant_path, encoding="utf-8") as handle:
        grant = json.load(handle)
    if not isinstance(grant, dict):
        return 2
    system = grant.get("system")
    user = grant.get("user")
    if not isinstance(system, str) or not isinstance(user, str):
        return 2
    request = os.path.join(scratch, "request.json")
    _write_json(request, {"system": system, "user": user})
    response = os.path.join(scratch, "response.json")
    deadline = time.monotonic() + wait_seconds
    payload = None
    while time.monotonic() < deadline:
        payload = _read_json(response)
        if isinstance(payload, dict) and ("text" in payload or "error" in payload):
            break
        time.sleep(0.05)
    if not isinstance(payload, dict):
        return 3
    if payload.get("error"):
        _write_json(os.path.join(scratch, "output.json"), {"error": str(payload.get("error"))})
        return 4
    text = payload.get("text")
    if not isinstance(text, str):
        return 4
    _write_json(os.path.join(scratch, "output.json"), {"text": text})
    return 0


def _write_json(path: str, payload: dict) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temporary, path)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


if __name__ == "__main__":
    raise SystemExit(main())

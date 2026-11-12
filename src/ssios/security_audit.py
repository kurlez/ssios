"""Append-only hash-chained audit log.

Entries are metadata only. The writer rejects fields that look like secrets,
passwords, or message bodies. There is no rewrite API.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from ssios.errors import SsiosError
from ssios.util import canonical_json, secure_mkdir, sha256_bytes

_SECRET = re.compile(
    r"(ya29\.|refresh_token|client_secret|access_token|BEGIN [A-Z ]*PRIVATE|password|ssios_session|Bearer\s+\S)",
    re.IGNORECASE,
)
_ALLOWED_EXTRA = {
    "route",
    "source",
    "service",
    "item_count",
    "item_ids",
    "connected",
    "isolated",
    "backend",
    "role",
    "phase",
    "reason_code",
    "host",
    "http_status",
    "byte_count",
    "nic",
    "outbound",
    "marker",
    "sandbox_denied",
    "outside_write",
}


class SecurityAudit:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def ensure(self) -> None:
        secure_mkdir(self.path.parent)
        if not self.path.exists():
            self.path.touch()
            self.path.chmod(0o600)

    def write(
        self,
        *,
        actor: str,
        ip: str,
        action: str,
        target: str,
        result: str,
        extra: dict | None = None,
    ) -> dict:
        row = {
            "ts": round(time.time(), 3),
            "actor": _short(actor),
            "ip": _short(ip),
            "action": _short(action),
            "target": _short(target),
            "result": _short(result),
        }
        if extra:
            clean = {}
            for key, value in extra.items():
                if key not in _ALLOWED_EXTRA:
                    continue
                clean[key] = _short(value if not isinstance(value, bool) else ("true" if value else "false"))
            if clean:
                row["extra"] = clean
        _reject_sensitive(row)
        with self._lock:
            self.ensure()
            previous = _last_hash(self.path)
            body = {key: row[key] for key in ("ts", "actor", "ip", "action", "target", "result")}
            if "extra" in row:
                body["extra"] = row["extra"]
            body["prev"] = previous
            digest = sha256_bytes(canonical_json(body))
            body["hash"] = digest
            line = json.dumps(body, sort_keys=True, ensure_ascii=False)
            _reject_sensitive(body)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
            self.path.chmod(0o600)
        return body

    def records(self) -> list[dict]:
        if not self.path.is_file():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                rows.append({"_invalid": True})
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows

    def verify(self) -> dict:
        previous = "0" * 64
        count = 0
        for index, row in enumerate(self.records()):
            if row.get("_invalid"):
                return {"ok": False, "detail": f"line {index + 1} is not json", "entries": count}
            expected_prev = str(row.get("prev") or "")
            digest = str(row.get("hash") or "")
            material = {key: row.get(key) for key in ("ts", "actor", "ip", "action", "target", "result", "prev")}
            if isinstance(row.get("extra"), dict):
                material["extra"] = row["extra"]
            actual = sha256_bytes(canonical_json(material))
            if expected_prev != previous or digest != actual:
                return {"ok": False, "detail": f"chain broke at entry {index + 1}", "entries": count}
            previous = digest
            count += 1
        return {"ok": True, "detail": "chain ok", "entries": count}

    def filtered(self, *, actor: str = "", action: str = "", since: float | None = None, until: float | None = None) -> list[dict]:
        rows = []
        for row in self.records():
            if row.get("_invalid"):
                continue
            if actor and row.get("actor") != actor:
                continue
            if action and row.get("action") != action:
                continue
            ts = float(row.get("ts") or 0)
            if since is not None and ts < since:
                continue
            if until is not None and ts > until:
                continue
            rows.append(row)
        return rows


def _last_hash(path: Path) -> str:
    if not path.is_file():
        return "0" * 64
    last = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            last = line
    if not last:
        return "0" * 64
    try:
        row = json.loads(last)
    except json.JSONDecodeError:
        return "0" * 64
    digest = str(row.get("hash") or "")
    if re.fullmatch(r"[0-9a-f]{64}", digest):
        return digest
    return "0" * 64


def _short(value: object) -> str:
    text = str(value if value is not None else "")
    text = text.replace("\n", " ").replace("\r", " ")
    if len(text) > 80:
        raise SsiosError("audit_rejected", "audit field rejected", 500)
    _reject_text(text)
    return text


def _reject_text(text: str) -> None:
    if _SECRET.search(text):
        raise SsiosError("audit_rejected", "audit field rejected", 500)


def _reject_sensitive(row: dict) -> None:
    blob = json.dumps(row, ensure_ascii=False)
    _reject_text(blob)

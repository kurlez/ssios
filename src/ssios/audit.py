"""Append-only audit log of action metadata and payload hashes."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ssios.util import secure_mkdir


ALLOWED_FIELDS = {
    "ts",
    "actor_role",
    "actor_id",
    "action",
    "result",
    "task_id",
    "project_id",
    "approval_id",
    "payload_sha256",
    "query_sha256",
    "revision",
    "error_code",
    "replaces",
    "tool",
    "byte_count",
    "item_count",
    "truncated",
    "upstream_status",
}


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def ensure(self) -> None:
        secure_mkdir(self.path.parent)
        if not self.path.exists():
            fd = self.path.open("a", encoding="utf-8")
            fd.close()
            self.path.chmod(0o600)

    def write(self, **fields: object) -> None:
        row = {key: fields[key] for key in ALLOWED_FIELDS if key in fields and fields[key] is not None}
        line = json.dumps(row, sort_keys=True, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()

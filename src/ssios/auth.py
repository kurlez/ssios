"""Local bearer tokens. Raw secrets stay in mode 0600 files; the store keeps hashes."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from ssios.errors import SsiosError
from ssios.util import assert_private, atomic_write, secure_mkdir


@dataclass(frozen=True)
class Grant:
    project_id: str
    task_id: str
    input_revision: str
    input_paths: tuple[str, ...]
    allow_memory_read: bool = True
    allow_memory_propose: bool = True
    allow_storage_read: bool = True
    allow_storage_stage: bool = True

    def to_json(self) -> dict:
        return {
            "project_id": self.project_id,
            "task_id": self.task_id,
            "input_revision": self.input_revision,
            "input_paths": list(self.input_paths),
            "allow_memory_read": self.allow_memory_read,
            "allow_memory_propose": self.allow_memory_propose,
            "allow_storage_read": self.allow_storage_read,
            "allow_storage_stage": self.allow_storage_stage,
        }

    @classmethod
    def from_json(cls, data: dict | None) -> Grant | None:
        if not data:
            return None
        return cls(
            project_id=str(data["project_id"]),
            task_id=str(data["task_id"]),
            input_revision=str(data["input_revision"]),
            input_paths=tuple(data.get("input_paths") or ()),
            allow_memory_read=bool(data.get("allow_memory_read", False)),
            allow_memory_propose=bool(data.get("allow_memory_propose", False)),
            allow_storage_read=bool(data.get("allow_storage_read", False)),
            allow_storage_stage=bool(data.get("allow_storage_stage", False)),
        )


@dataclass(frozen=True)
class Principal:
    role: str
    token_id: str
    grant: Grant | None


class AuthStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.auth_dir = state_dir / "auth"
        self.secret_dir = state_dir / "secrets"
        self.map_path = self.auth_dir / "tokens.json"
        self._records: dict[str, dict] = {}

    def load(self) -> None:
        assert_private(self.auth_dir)
        assert_private(self.secret_dir)
        assert_private(self.map_path)
        self._records = json.loads(self.map_path.read_text(encoding="utf-8"))

    def init_static_tokens(self) -> None:
        secure_mkdir(self.auth_dir)
        secure_mkdir(self.secret_dir)
        records: dict[str, dict] = {}
        for role, token_id in (("human", "human"), ("worker", "worker")):
            raw = secrets.token_urlsafe(32)
            digest = self._hash(raw)
            records[digest] = {"role": role, "token_id": token_id, "grant": None}
            atomic_write(self.secret_dir / f"{role}.token", (raw + "\n").encode("utf-8"), 0o600)
        atomic_write(self.map_path, (json.dumps(records, indent=2, sort_keys=True) + "\n").encode(), 0o600)
        self._records = records

    def read_secret(self, role: str) -> str:
        if role not in ("human", "worker"):
            raise SsiosError("forbidden", "unknown static role", 403)
        path = self.secret_dir / f"{role}.token"
        assert_private(path)
        return path.read_text(encoding="utf-8").strip()

    def issue_task_token(self, grant: Grant) -> str:
        raw = secrets.token_urlsafe(32)
        digest = self._hash(raw)
        self._records[digest] = {
            "role": "task",
            "token_id": grant.task_id,
            "grant": grant.to_json(),
        }
        self._persist()
        return raw

    def lookup(self, token: str) -> Principal | None:
        if not token:
            return None
        record = self._records.get(self._hash(token))
        if record is None:
            return None
        return Principal(
            role=str(record["role"]),
            token_id=str(record["token_id"]),
            grant=Grant.from_json(record.get("grant")),
        )

    def _persist(self) -> None:
        atomic_write(self.map_path, (json.dumps(self._records, indent=2, sort_keys=True) + "\n").encode(), 0o600)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

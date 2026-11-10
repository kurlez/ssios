"""One-time approvals bound to an action, payload hash, and revision."""

from __future__ import annotations

import secrets
from pathlib import Path

from ssios.errors import SsiosError
from ssios.util import canonical_json, read_json, sha256_bytes, write_json


class ApprovalStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: dict[str, dict] = {}

    def load(self) -> None:
        data = read_json(self.path, {"approvals": {}})
        if not isinstance(data, dict):
            data = {"approvals": {}}
        self._items = dict(data.get("approvals") or {})

    def save(self) -> None:
        write_json(self.path, {"approvals": self._items})

    def create(
        self,
        *,
        action: str,
        project_id: str,
        task_id: str,
        payload_sha256: str,
        expected_revision: str,
        proposal_id: str | None,
        now: float,
        ttl_seconds: float,
    ) -> dict:
        approval_id = "appr-" + secrets.token_hex(8)
        nonce = secrets.token_hex(16)
        material = {
            "action": action,
            "approval_id": approval_id,
            "expected_revision": expected_revision,
            "nonce": nonce,
            "payload_sha256": payload_sha256,
            "project_id": project_id,
            "proposal_id": proposal_id,
            "task_id": task_id,
        }
        record = {
            **material,
            "bound_hash": sha256_bytes(canonical_json(material)),
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "status": "pending",
            "decision": None,
        }
        self._items[approval_id] = record
        self.save()
        return self.public(record)

    def records(self) -> list[dict]:
        return list(self._items.values())

    def get(self, approval_id: str) -> dict:
        record = self._items.get(approval_id)
        if record is None:
            raise SsiosError("not_found", "approval not found", 404)
        return record

    def public(self, record: dict) -> dict:
        return {
            "id": record["approval_id"],
            "action": record["action"],
            "project_id": record["project_id"],
            "task_id": record["task_id"],
            "proposal_id": record.get("proposal_id"),
            "payload_sha256": record["payload_sha256"],
            "expected_revision": record["expected_revision"],
            "bound_hash": record["bound_hash"],
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
            "status": record["status"],
            "decision": record.get("decision"),
        }

    def binding_matches(self, record: dict, body: dict) -> bool:
        keys = ("action", "payload_sha256", "expected_revision", "bound_hash")
        return all(body.get(key) == record.get(key) for key in keys)

    def mark(self, approval_id: str, *, status: str, decision: str | None) -> dict:
        record = self.get(approval_id)
        record["status"] = status
        record["decision"] = decision
        self.save()
        return record

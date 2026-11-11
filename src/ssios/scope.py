"""Server-side account scope.

Every memory, approval, chat, and workspace path is derived from the
authenticated account id. A member never receives the legacy owner's store.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from ssios.approvals import ApprovalStore
from ssios.chat_store import ChatStore
from ssios.errors import SsiosError
from ssios.memory_store import MemoryStore
from ssios.util import read_json, secure_mkdir, sha256_bytes, write_json, write_nofollow

MEMBER_PROJECT = "notes"
LEGACY_PROJECT = "local-summary"


class AccountScope:
    def __init__(self, plane, account: dict) -> None:
        if not isinstance(account, dict) or not account.get("id"):
            raise SsiosError("unauthorized", "authentication required", 401)
        self.plane = plane
        self.account = account
        self.account_id = str(account["id"])
        self.legacy = bool(account.get("legacy_owner"))
        self.home = plane.accounts.home(self.account_id)
        self._ensure()

    def _ensure(self) -> None:
        secure_mkdir(self.home)
        for name in ("workspace", "accepted", "chat", "staged"):
            secure_mkdir(self.home / name)
        if not self.approvals_path.exists():
            write_json(self.approvals_path, {"approvals": {}})
        if not self.legacy:
            seed_member_memory(self.plane, self.home)

    @property
    def approvals_path(self) -> Path:
        return self.home / "approvals.json"

    def chat(self) -> ChatStore:
        return ChatStore(self.home)

    def approvals(self) -> ApprovalStore:
        store = ApprovalStore(self.approvals_path)
        store.load()
        return store

    def memory(self) -> MemoryStore:
        if self.legacy:
            return self.plane.memory
        return MemoryStore(self.home, self.plane.git, self.plane.fixtures)

    def project_id(self) -> str:
        return LEGACY_PROJECT if self.legacy else MEMBER_PROJECT

    def has_approval(self, approval_id: str) -> bool:
        try:
            self.approvals().get(approval_id)
        except SsiosError:
            return False
        return True

    def list_pending(self) -> list[dict]:
        return [item for item in self.approvals().records() if item.get("status") == "pending"]

    def preview(self, record: dict) -> str:
        action = record.get("action")
        if action == "memory.commit":
            proposal = self.memory().load_proposal(str(record.get("proposal_id") or ""))
            return "\n\n".join(str(item.get("content") or "") for item in proposal.get("files") or [])
        if action in ("chat.accept", "connector.fetch"):
            return self.staged_bytes(str(record.get("proposal_id") or "")).decode("utf-8", "replace")
        return ""

    def stage_bytes(self, proposal_id: str, data: bytes, meta: dict) -> str:
        if "/" in proposal_id or ".." in proposal_id:
            raise SsiosError("path_rejected", "proposal id rejected", 400)
        write_nofollow(self.home / "staged", proposal_id + ".bin", data)
        write_json(self.home / "staged" / f"{proposal_id}.json", meta)
        return sha256_bytes(data)

    def staged_bytes(self, proposal_id: str) -> bytes:
        from ssios.util import read_nofollow

        return read_nofollow(self.home / "staged", proposal_id + ".bin")

    def staged_meta(self, proposal_id: str) -> dict:
        data = read_json(self.home / "staged" / f"{proposal_id}.json", {})
        return data if isinstance(data, dict) else {}

    def create_memory_approval(self, proposal: dict) -> dict:
        return self.approvals().create(
            action="memory.commit",
            project_id=self.project_id(),
            task_id="chat",
            payload_sha256=str(proposal["payload_sha256"]),
            expected_revision=str(proposal["expected_revision"]),
            proposal_id=str(proposal["id"]),
            now=time.time(),
            ttl_seconds=self.plane.settings.approval_ttl_seconds,
        )

    def create_approval(self, *, action: str, project_id: str, task_id: str, payload: bytes, meta: dict) -> dict:
        proposal_id = "prop-" + secrets.token_hex(8)
        digest = self.stage_bytes(proposal_id, payload, meta)
        return self.approvals().create(
            action=action,
            project_id=project_id,
            task_id=task_id,
            payload_sha256=digest,
            expected_revision=str(meta.get("expected_revision") or "chat"),
            proposal_id=proposal_id,
            now=time.time(),
            ttl_seconds=self.plane.settings.approval_ttl_seconds,
        )

    def decide(self, approval_id: str, body: dict, peer_ip: str) -> dict:
        store = self.approvals()
        record = store.get(approval_id)
        if record["status"] == "pending" and time.time() >= float(record["expires_at"]):
            record["decided_at"] = time.time()
            store.mark(approval_id, status="expired", decision=None)
            raise SsiosError("approval_expired", "approval expired", 409)
        if record["status"] != "pending":
            raise SsiosError("approval_replay_denied", "approval is no longer pending", 409)
        if not store.binding_matches(record, body):
            self._audit(peer_ip, "approval.tamper", approval_id, "denied")
            raise SsiosError("approval_binding_mismatch", "approval binding does not match", 409)
        decision = body.get("decision")
        if decision not in ("approve", "deny"):
            raise SsiosError("approval_invalid", "decision rejected", 400)
        if decision == "deny":
            record["decided_at"] = time.time()
            store.mark(approval_id, status="denied", decision="deny")
            self._audit(peer_ip, "approval.deny", record["action"], "denied")
            return {"status": "denied", "action": record["action"]}
        applied = self._apply(record, peer_ip)
        record["decided_at"] = time.time()
        store.mark(approval_id, status="consumed", decision="approve")
        self._audit(peer_ip, "approval.approve", record["action"], "ok")
        applied["status"] = "consumed"
        return applied

    def _apply(self, record: dict, peer_ip: str) -> dict:
        action = record["action"]
        if action == "chat.accept":
            data = self.staged_bytes(str(record["proposal_id"]))
            if sha256_bytes(data) != record["payload_sha256"]:
                raise SsiosError("payload_mismatch", "staged bytes do not match the approval", 409)
            write_nofollow(self.home / "accepted", str(record["approval_id"]) + ".md", data, mode=0o600)
            return {"action": action, "meta": self.staged_meta(str(record["proposal_id"]))}
        if action == "vault.clip":
            data = self.staged_bytes(str(record["proposal_id"]))
            if sha256_bytes(data) != record["payload_sha256"]:
                raise SsiosError("payload_mismatch", "staged bytes do not match the approval", 409)
            saved = self.plane.skills.commit_clip(self.account_id, data.decode("utf-8"), self.staged_meta(str(record["proposal_id"])))
            return {"action": action, "path": saved.get("path"), "meta": saved}
        if action == "memory.commit":
            revision = self.memory().apply(
                str(record["proposal_id"]),
                str(record["expected_revision"]),
                str(record["approval_id"]),
                str(record["payload_sha256"]),
            )
            return {"action": action, "revision": revision, "meta": {}}
        if action == "mcp.write":
            from ssios.mcp_host import apply_write

            return apply_write(self, record)
        raise SsiosError("approval_invalid", "action rejected", 400)

    def _audit(self, peer_ip: str, action: str, target: str, result: str) -> None:
        self.plane.security_audit.write(actor=self.account_id, ip=peer_ip or "127.0.0.1", action=action, target=target, result=result)

    def tasks(self) -> dict:
        data = read_json(self.home / "tasks.json", {"tasks": {}})
        tasks = data.get("tasks") if isinstance(data, dict) else {}
        return tasks if isinstance(tasks, dict) else {}

    def save_task(self, task: dict) -> None:
        tasks = self.tasks()
        tasks[str(task["id"])] = {
            key: task.get(key)
            for key in ("id", "status", "backend", "isolated", "boot_ms", "created_at", "error_code", "error_detail")
        }
        write_json(self.home / "tasks.json", {"tasks": tasks})


def seed_member_memory(plane, home: Path) -> None:
    store = MemoryStore(home, plane.git, plane.fixtures)
    project = MEMBER_PROJECT
    repo = store.root / project / "repo"
    if (repo / ".git").is_dir():
        return
    secure_mkdir(repo)
    files = {
        "MEMORY.md": "# Memory\n",
        "human.md": "---\nname: human\ndescription: \"profile\"\n---\n\nThe person I'm working with. Learn more about the person.\n",
        "persona.md": "---\nname: persona\ndescription: \"persona\"\n---\n\nI am a helpful assistant.\n",
        "project.md": "---\nname: project\ndescription: \"account project\"\n---\n\n",
        "preferences.md": "---\nname: preferences\ndescription: \"preferences\"\n---\n\n",
    }
    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
    plane.git.init_repo(repo)
    revision = plane.git.commit_paths(repo, list(files), "ssios seed account memory")
    provenance = {"project_id": project, "files": {}}
    for name in files:
        blob = plane.git.run(repo, ["show", f"HEAD:{name}"]).stdout
        provenance["files"][name] = {
            "trust": "synthetic_seed",
            "source": "account_seed",
            "payload_sha256": sha256_bytes(blob),
            "revision": revision,
            "tombstone": False,
        }
    write_json(store.provenance_path(project), provenance)


def memory_excerpt(scope: AccountScope, query: str = "") -> str:
    from ssios.account_memory import memory_context

    text, _files = memory_context(scope, query)
    return text

"""Trusted control plane: authz, tasks, approvals, memory, and storage."""

from __future__ import annotations

import secrets
import threading
import time
import traceback

from ssios import __version__
from ssios.agent import AgentRunner
from ssios.approvals import ApprovalStore
from ssios.audit import AuditLog
from ssios.auth import AuthStore, Grant, Principal
from ssios.errors import FlowInterrupt, SsiosError
from ssios.gitio import GitRunner
from ssios.llm import build_llm
from ssios.memory_store import MemoryStore, validate_document
from ssios.paths import fixtures_dir
from ssios.runtime import CloudHypervisorRuntime, DevGuardRuntime
from ssios.storage import ProjectStorage
from ssios.upstream import describe_memfs
from ssios.util import assert_private, atomic_write, read_json, secure_mkdir, sha256_bytes, write_json

TASK_FIELDS = (
    "id",
    "project_id",
    "kind",
    "status",
    "created_at",
    "input_revision",
    "input_path",
    "tool_trace",
    "approval_ids",
    "error_code",
    "published_revision",
    "memory_revision",
    "output_sha256",
)


class ControlPlane:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.lock = threading.RLock()
        self._offset = 0.0
        self.bound_port = settings.port
        self.fixtures = fixtures_dir()
        self.auth = AuthStore(settings.state_dir)
        self.audit = AuditLog(settings.state_dir / "audit" / "audit.jsonl")
        self.approvals = ApprovalStore(settings.state_dir / "approvals.json")
        self.git = GitRunner(settings.state_dir / "git-hooks", settings.state_dir / "empty-gitconfig")
        self.memory = MemoryStore(settings.state_dir, self.git, self.fixtures)
        self.storage = ProjectStorage(settings.state_dir, self.fixtures)
        self.llm = build_llm(settings)
        self.dev = DevGuardRuntime()
        self.cloud = CloudHypervisorRuntime(settings)
        self.runner = AgentRunner(self)
        self.tasks: dict[str, dict] = {}
        self._tokens: dict[str, str] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._user_cancel: dict[str, bool] = {}
        self._threads: dict[str, threading.Thread] = {}

    def now(self) -> float:
        return time.time() + self._offset

    def advance_clock(self, seconds: float) -> None:
        self._offset += seconds

    def user_cancelled(self, task_id: str) -> bool:
        return self._user_cancel.get(task_id, False)

    def open(self) -> None:
        self._ensure_layout()
        self.auth.load()
        self.approvals.load()
        self._load_tasks()
        self._check_perms()

    def _ensure_layout(self) -> None:
        state = self.settings.state_dir
        secure_mkdir(state)
        secure_mkdir(state / "git-hooks")
        secure_mkdir(state / "memory" / "proposals")
        secure_mkdir(state / "audit")
        if not (state / "empty-gitconfig").exists():
            atomic_write(state / "empty-gitconfig", b"")
        self.audit.ensure()
        if not self.auth.map_path.exists():
            self.auth.init_static_tokens()
        if not self.approvals.path.exists():
            write_json(self.approvals.path, {"approvals": {}})
        if not (state / "tasks.json").exists():
            write_json(state / "tasks.json", {"tasks": {}})
        for project_id in ("demo-alpha", "demo-beta"):
            if not (self.memory.root / project_id / "repo" / ".git").exists():
                self.memory.seed_project(project_id)
            if not (self.storage.exports / project_id / "exp-1" / "manifest.json").exists():
                self.storage.seed_export(project_id, "exp-1")
        vault = state / "vault" / "canary.txt"
        if not vault.exists():
            data = (self.fixtures / "vault" / "canary.txt").read_bytes()
            atomic_write(vault, data, 0o600)

    def _check_perms(self) -> None:
        state = self.settings.state_dir
        for path in (
            state,
            state / "auth",
            state / "secrets",
            state / "vault",
            self.auth.map_path,
            self.auth.secret_dir / "human.token",
            self.auth.secret_dir / "worker.token",
            state / "vault" / "canary.txt",
        ):
            assert_private(path)

    def _load_tasks(self) -> None:
        data = read_json(self.settings.state_dir / "tasks.json", {"tasks": {}})
        tasks = dict(data.get("tasks") or {})
        for task in tasks.values():
            if task.get("status") in ("queued", "running"):
                task["status"] = "interrupted"
                task["error_code"] = "interrupted"
        self.tasks = tasks
        self._persist_tasks()

    def _persist_tasks(self) -> None:
        public = {task_id: {key: task.get(key) for key in TASK_FIELDS} for task_id, task in self.tasks.items()}
        write_json(self.settings.state_dir / "tasks.json", {"tasks": public})

    def health(self, token: str) -> dict:
        self.require_principal(token)
        return {
            "status": "ok",
            "version": __version__,
            "profile": self.settings.profile,
            "llm_provider": self.settings.llm_provider,
            "runtime_backend": self.settings.runtime_backend,
            "memory_mode": "letta-memfs-compat",
        }

    def status(self, token: str) -> dict:
        self.require_principal(token)
        return {
            "version": __version__,
            "profile": self.settings.profile,
            "bind": f"{self.settings.host}:{self.bound_port}",
            "telemetry": False,
            "outbound": "deny",
            "memory": describe_memfs(),
            "llm": {
                "provider": self.settings.llm_provider,
                "deterministic": bool(getattr(self.llm, "deterministic", False)),
                "loopback_only": True,
                "external_fallback": False,
                "model": self.settings.ollama_model if self.settings.llm_provider == "ollama" else "mock",
            },
            "runtime": {
                "active_backend": self.settings.runtime_backend,
                "dev_guard": self.dev.describe(),
                "cloud_hypervisor": self.cloud.preflight(),
            },
            "approvals": "separate-human-token",
        }

    def preflight(self, token: str) -> dict:
        self.require_human(token)
        return {
            "active_backend": self.settings.runtime_backend,
            "dev_guard": self.dev.describe(),
            "cloud_hypervisor": self.cloud.preflight(),
        }

    def require_principal(self, token: str) -> Principal:
        principal = self.auth.lookup(token)
        if principal is None:
            raise SsiosError("unauthorized", "authentication required", 401)
        return principal

    def require_human(self, token: str, *, approval: bool = False) -> Principal:
        principal = self.require_principal(token)
        if principal.role != "human":
            code = "worker_cannot_approve" if approval else "forbidden"
            raise SsiosError(code, "human control token required", 403)
        return principal

    def _grant(self, token: str, project_id: str, flag: str) -> Principal:
        principal = self.require_principal(token)
        if principal.role == "human":
            return principal
        grant = principal.grant
        if principal.role != "task" or grant is None:
            raise SsiosError("forbidden", "no project grant", 403)
        if grant.project_id != project_id:
            raise SsiosError("cross_project_denied", "grant does not include this project", 403)
        if not getattr(grant, flag):
            raise SsiosError("forbidden", "operation is not granted", 403)
        return principal

    def read_core(self, token: str, project_id: str) -> dict:
        with self.lock:
            principal = self._grant(token, project_id, "allow_memory_read")
            return self.memory.core(
                project_id,
                budget=self.settings.core_token_budget,
                include_untrusted=principal.role == "human",
            )

    def read_memory(self, token: str, project_id: str, path: str) -> dict:
        with self.lock:
            principal = self._grant(token, project_id, "allow_memory_read")
            return self.memory.read_file(project_id, path, include_untrusted=principal.role == "human")

    def search_memory(self, token: str, project_id: str, query: str) -> dict:
        with self.lock:
            principal = self._grant(token, project_id, "allow_memory_read")
            result = self.memory.search(project_id, query, include_untrusted=principal.role == "human")
            self.audit.write(
                ts=self.now(),
                actor_role=principal.role,
                actor_id=principal.token_id,
                action="memory.search",
                result="ok",
                project_id=project_id,
                query_sha256=result["query_sha256"],
                revision=result["revision"],
            )
            return result

    def read_input(self, token: str, project_id: str, revision: str, path: str) -> dict:
        with self.lock:
            principal = self._grant(token, project_id, "allow_storage_read")
            if principal.role == "task":
                grant = principal.grant
                assert grant is not None
                if revision != grant.input_revision or path not in grant.input_paths:
                    raise SsiosError("path_not_granted", "input path is outside the task grant", 403)
            return self.storage.read_export(project_id, revision, path)

    def read_published(self, token: str, project_id: str, revision: str) -> dict:
        with self.lock:
            self.require_human(token)
            return self.storage.read_published(project_id, revision)

    def create_task(self, token: str, body: dict) -> dict:
        with self.lock:
            self.require_human(token)
            if self.settings.profile != "dev" or self.settings.runtime_backend != "dev-guard":
                self.cloud.launch()
            project_id = str(body.get("project_id", ""))
            kind = str(body.get("kind", ""))
            revision = str(body.get("input_revision", ""))
            input_path = str(body.get("input_path", ""))
            if kind not in ("project-summary", "slow", "shell-attempt"):
                raise SsiosError("task_rejected", "task kind rejected", 400)
            if kind != "project-summary" and self.settings.llm_provider != "mock":
                raise SsiosError("task_rejected", "this task kind requires the mock provider", 400)
            manifest = self.storage.manifest(project_id, revision)
            if input_path not in manifest.get("files", {}):
                raise SsiosError("not_found", "input path is not in the export", 404)
            task_id = "task-" + secrets.token_hex(8)
            task = {
                "id": task_id,
                "project_id": project_id,
                "kind": kind,
                "status": "queued",
                "created_at": self.now(),
                "input_revision": revision,
                "input_path": input_path,
                "tool_trace": [],
                "approval_ids": [],
                "error_code": None,
                "published_revision": None,
                "memory_revision": None,
                "output_sha256": None,
            }
            self.tasks[task_id] = task
            grant = Grant(
                project_id=project_id,
                task_id=task_id,
                input_revision=revision,
                input_paths=(input_path,),
            )
            self._tokens[task_id] = self.auth.issue_task_token(grant)
            self._cancels[task_id] = threading.Event()
            self._user_cancel[task_id] = False
            self.storage.task_dir(task_id)
            self._persist_tasks()
            self.audit.write(
                ts=self.now(),
                actor_role="human",
                action="task.create",
                result="ok",
                task_id=task_id,
                project_id=project_id,
                revision=revision,
            )
            thread = threading.Thread(target=self._execute, args=(task_id,), name=task_id, daemon=True)
            self._threads[task_id] = thread
            thread.start()
            return self.public_task(task)

    def get_task(self, token: str, task_id: str) -> dict:
        with self.lock:
            self.require_human(token)
            task = self._require_task(task_id)
            public = self.public_task(task)
            public["approvals"] = [self.approvals.public(self.approvals.get(item)) for item in task["approval_ids"]]
            return public

    def cancel_task(self, token: str, task_id: str) -> dict:
        with self.lock:
            self.require_human(token)
            task = self._require_task(task_id)
            if task["status"] not in ("queued", "running", "awaiting_approval"):
                raise SsiosError("task_not_running", "task cannot be cancelled", 409)
            self._user_cancel[task_id] = True
            event = self._cancels.get(task_id)
            if event is not None:
                event.set()
            if task["status"] == "awaiting_approval":
                task["status"] = "cancelled"
                task["error_code"] = "cancelled"
                self._persist_tasks()
            self.audit.write(
                ts=self.now(),
                actor_role="human",
                action="task.cancel",
                result="ok",
                task_id=task_id,
                project_id=task["project_id"],
            )
            return self.public_task(task)

    def propose_memory(self, token: str, body: dict) -> dict:
        with self.lock:
            project_id = str(body.get("project_id", ""))
            principal = self._grant(token, project_id, "allow_memory_propose")
            files = body.get("files")
            if not isinstance(files, list):
                raise SsiosError("memory_invalid", "files rejected", 400)
            for item in files:
                validate_document(str(item.get("path", "")), str(item.get("content", "")))
            proposal = self.memory.propose(
                project_id,
                str(body.get("expected_revision", "")),
                files,
                task_id=principal.grant.task_id if principal.grant else "human",
            )
            approval = self._approval(
                action="memory.commit",
                project_id=project_id,
                task_id=proposal["task_id"],
                payload_sha256=proposal["payload_sha256"],
                expected_revision=proposal["expected_revision"],
                proposal_id=proposal["id"],
            )
            self.audit.write(
                ts=self.now(),
                actor_role=principal.role,
                action="memory.propose",
                result="staged",
                project_id=project_id,
                approval_id=approval["id"],
                payload_sha256=proposal["payload_sha256"],
                revision=proposal["expected_revision"],
            )
            return {"proposal_id": proposal["id"], "approval": approval}

    def forget_memory(self, token: str, body: dict) -> dict:
        with self.lock:
            project_id = str(body.get("project_id", ""))
            principal = self._grant(token, project_id, "allow_memory_propose")
            proposal = self.memory.propose_forget(
                project_id,
                str(body.get("expected_revision", "")),
                str(body.get("path", "")),
                task_id=principal.grant.task_id if principal.grant else "human",
            )
            approval = self._approval(
                action="memory.forget",
                project_id=project_id,
                task_id=proposal["task_id"],
                payload_sha256=proposal["payload_sha256"],
                expected_revision=proposal["expected_revision"],
                proposal_id=proposal["id"],
            )
            self.audit.write(
                ts=self.now(),
                actor_role=principal.role,
                action="memory.forget",
                result="staged",
                project_id=project_id,
                approval_id=approval["id"],
                payload_sha256=proposal["payload_sha256"],
                revision=proposal["expected_revision"],
            )
            return {"proposal_id": proposal["id"], "approval": approval}

    def get_approval(self, token: str, approval_id: str) -> dict:
        with self.lock:
            self.require_human(token)
            record = self.approvals.get(approval_id)
            preview = self._preview_text(record)
            self.audit.write(
                ts=self.now(),
                actor_role="human",
                action="approval.preview",
                result="ok",
                task_id=record["task_id"],
                project_id=record["project_id"],
                approval_id=approval_id,
                payload_sha256=record["payload_sha256"],
            )
            public = self.approvals.public(record)
            public["preview"] = preview
            return public

    def decide(self, token: str, approval_id: str, body: dict) -> dict:
        with self.lock:
            self.require_human(token, approval=True)
            record = self.approvals.get(approval_id)
            if record["status"] != "pending":
                self.audit.write(
                    ts=self.now(),
                    actor_role="human",
                    action="approval.decide",
                    result="denied",
                    approval_id=approval_id,
                    error_code="approval_replay_denied",
                    task_id=record["task_id"],
                    project_id=record["project_id"],
                )
                raise SsiosError("approval_replay_denied", "approval is no longer pending", 409)
            if self.now() >= float(record["expires_at"]):
                self.approvals.mark(approval_id, status="expired", decision=None)
                self.audit.write(
                    ts=self.now(),
                    actor_role="human",
                    action="approval.decide",
                    result="denied",
                    approval_id=approval_id,
                    error_code="approval_expired",
                    payload_sha256=record["payload_sha256"],
                )
                raise SsiosError("approval_expired", "approval expired", 409)
            if not self.approvals.binding_matches(record, body):
                self.audit.write(
                    ts=self.now(),
                    actor_role="human",
                    action="approval.tamper",
                    result="denied",
                    approval_id=approval_id,
                    error_code="approval_binding_mismatch",
                    task_id=record["task_id"],
                    project_id=record["project_id"],
                )
                raise SsiosError("approval_binding_mismatch", "approval binding does not match", 409)
            task = self.tasks.get(record["task_id"])
            if task is not None and task["status"] in ("cancelled", "timeout", "failed", "rejected"):
                raise SsiosError("task_closed", "task is closed", 409)
            decision = body.get("decision")
            if decision not in ("approve", "deny"):
                raise SsiosError("approval_invalid", "decision rejected", 400)
            if decision == "deny":
                self.approvals.mark(approval_id, status="denied", decision="deny")
                if record.get("proposal_id"):
                    self.memory.mark_proposal(record["proposal_id"], "rejected")
                self._refresh_task(record["task_id"])
                self.audit.write(
                    ts=self.now(),
                    actor_role="human",
                    action="approval.decide",
                    result="denied",
                    approval_id=approval_id,
                    payload_sha256=record["payload_sha256"],
                    task_id=record["task_id"],
                    project_id=record["project_id"],
                )
                return self.approvals.public(self.approvals.get(approval_id))
            try:
                applied = self._apply(record)
            except SsiosError as exc:
                if exc.code == "cas_conflict":
                    self.approvals.mark(approval_id, status="conflict", decision="approve")
                    if record.get("proposal_id"):
                        self.memory.mark_proposal(record["proposal_id"], "conflict")
                elif exc.code == "payload_mismatch":
                    self.approvals.mark(approval_id, status="tampered", decision="approve")
                else:
                    raise
                self._refresh_task(record["task_id"])
                self.audit.write(
                    ts=self.now(),
                    actor_role="human",
                    action="approval.decide",
                    result="error",
                    approval_id=approval_id,
                    error_code=exc.code,
                    payload_sha256=record["payload_sha256"],
                    task_id=record["task_id"],
                    project_id=record["project_id"],
                )
                raise
            self.approvals.mark(approval_id, status="consumed", decision="approve")
            self._refresh_task(record["task_id"])
            self.audit.write(
                ts=self.now(),
                actor_role="human",
                action="approval.decide",
                result="ok",
                approval_id=approval_id,
                payload_sha256=record["payload_sha256"],
                revision=applied,
                task_id=record["task_id"],
                project_id=record["project_id"],
            )
            public = self.approvals.public(self.approvals.get(approval_id))
            public["applied_revision"] = applied
            return public

    def stage_again(self, token: str, task_id: str, data: bytes) -> dict:
        with self.lock:
            principal = self.require_principal(token)
            grant = principal.grant
            if principal.role != "task" or grant is None or grant.task_id != task_id or not grant.allow_storage_stage:
                raise SsiosError("forbidden", "staging is not granted", 403)
            if self.storage.is_frozen(task_id):
                raise SsiosError("staging_frozen", "staging is frozen", 409)
            return self.storage.stage(task_id, "summary.md", data)

    def issue_task_token(self, project_id: str, *, paths: tuple[str, ...], revision: str) -> str:
        with self.lock:
            task_id = "task-" + secrets.token_hex(8)
            return self.auth.issue_task_token(
                Grant(project_id=project_id, task_id=task_id, input_revision=revision, input_paths=paths)
            )

    def public_task(self, task: dict) -> dict:
        return {key: task.get(key) for key in TASK_FIELDS}

    def _execute(self, task_id: str) -> None:
        with self.lock:
            task = self.tasks[task_id]
            task["status"] = "running"
            self._persist_tasks()
            token = self._tokens[task_id]
            cancel_event = self._cancels[task_id]
        deadline = time.monotonic() + self.settings.task_timeout_seconds
        self._arm(cancel_event, deadline)
        try:
            result = self.runner.run(task, token, cancel_event, deadline)
            self._finish(task_id, token, result)
        except FlowInterrupt:
            kind = "cancelled" if self.user_cancelled(task_id) else "timeout"
            self._close_task(task_id, kind, kind)
        except SsiosError as exc:
            self._close_task(task_id, "failed", exc.code)
        except Exception:
            traceback.print_exc()
            self._close_task(task_id, "failed", "internal")

    def _finish(self, task_id: str, token: str, result: dict) -> None:
        with self.lock:
            if self.user_cancelled(task_id):
                self._close_task_unlocked(task_id, "cancelled", "cancelled")
                return
            task = self.tasks[task_id]
            encoded = result["final_text"].encode("utf-8")
            staged = self.storage.stage(task_id, "summary.md", encoded)
            self.storage.freeze(task_id)
            topic = self.memory.read_file(task["project_id"], "topics/fixtures/MEMORY.md", include_untrusted=False)
            index = topic["content"]
            if "last-summary.md" not in index:
                index = index.rstrip() + "\n- last-summary.md — approved task summary\n"
            summary_doc = (
                "---\n"
                "name: last-summary\n"
                'description: "Approved synthetic task summary."\n'
                "---\n\n"
                f"{result['final_text']}\n"
            )
            revision = self.memory.head(task["project_id"])
            proposal = self.memory.propose(
                task["project_id"],
                revision,
                [
                    {"path": "topics/fixtures/last-summary.md", "content": summary_doc},
                    {"path": "topics/fixtures/MEMORY.md", "content": index},
                ],
                task_id=task_id,
            )
            publish = self._approval(
                action="storage.publish",
                project_id=task["project_id"],
                task_id=task_id,
                payload_sha256=staged["sha256"],
                expected_revision=self.storage.published_head(task["project_id"]),
                proposal_id=None,
            )
            memory_approval = self._approval(
                action="memory.commit",
                project_id=task["project_id"],
                task_id=task_id,
                payload_sha256=proposal["payload_sha256"],
                expected_revision=revision,
                proposal_id=proposal["id"],
            )
            task["tool_trace"] = list(result["trace"])
            task["output_sha256"] = staged["sha256"]
            task["approval_ids"] = [publish["id"], memory_approval["id"]]
            task["status"] = "awaiting_approval"
            task["error_code"] = None
            self._persist_tasks()
            self.audit.write(
                ts=self.now(),
                actor_role="task",
                actor_id=task_id,
                action="task.stage",
                result="ok",
                task_id=task_id,
                project_id=task["project_id"],
                payload_sha256=staged["sha256"],
            )
            del token

    def _close_task(self, task_id: str, status: str, code: str) -> None:
        with self.lock:
            self._close_task_unlocked(task_id, status, code)

    def _close_task_unlocked(self, task_id: str, status: str, code: str) -> None:
        task = self.tasks[task_id]
        task["status"] = status
        task["error_code"] = code
        self._persist_tasks()
        self.audit.write(
            ts=self.now(),
            actor_role="task",
            actor_id=task_id,
            action=f"task.{status}",
            result=status,
            task_id=task_id,
            project_id=task["project_id"],
            error_code=code,
        )

    def _arm(self, cancel_event: threading.Event, deadline: float) -> None:
        def watch() -> None:
            remaining = deadline - time.monotonic()
            if cancel_event.wait(max(0.0, remaining)):
                return
            cancel_event.set()

        threading.Thread(target=watch, daemon=True).start()

    def _approval(self, **kwargs) -> dict:
        return self.approvals.create(now=self.now(), ttl_seconds=self.settings.approval_ttl_seconds, **kwargs)

    def _apply(self, record: dict) -> str:
        action = record["action"]
        if action == "storage.publish":
            revision = self.storage.publish(
                record["project_id"],
                record["task_id"],
                record["expected_revision"],
                record["payload_sha256"],
            )
            task = self.tasks.get(record["task_id"])
            if task is not None:
                task["published_revision"] = revision
            return revision
        if action in ("memory.commit", "memory.forget"):
            revision = self.memory.apply(
                record["proposal_id"],
                record["expected_revision"],
                record["approval_id"],
                record["payload_sha256"],
            )
            task = self.tasks.get(record["task_id"])
            if task is not None and action == "memory.commit":
                task["memory_revision"] = revision
            return revision
        raise SsiosError("approval_invalid", "action rejected", 400)

    def _preview_text(self, record: dict) -> str:
        if record["action"] == "storage.publish":
            data = self.storage.staged_bytes(record["task_id"])
            if sha256_bytes(data) != record["payload_sha256"]:
                raise SsiosError("payload_mismatch", "staged bytes do not match the approval", 409)
            return data.decode("utf-8")
        proposal = self.memory.load_proposal(record["proposal_id"])
        if record["action"] == "memory.forget":
            return proposal["path"]
        return "\n\n".join(item["content"] for item in proposal["files"])

    def _refresh_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task is None or not task["approval_ids"]:
            return
        records = [self.approvals.get(item) for item in task["approval_ids"]]
        if any(item["status"] == "pending" for item in records):
            if task["status"] not in ("cancelled", "timeout", "failed"):
                task["status"] = "awaiting_approval"
        elif all(item["status"] == "consumed" and item["decision"] == "approve" for item in records):
            task["status"] = "committed"
            task["error_code"] = None
        elif any(item["status"] == "conflict" for item in records):
            task["status"] = "failed"
            task["error_code"] = "cas_conflict"
        elif any(item["status"] == "tampered" for item in records):
            task["status"] = "failed"
            task["error_code"] = "payload_mismatch"
        elif any(item["status"] == "denied" for item in records):
            task["status"] = "rejected"
            task["error_code"] = "denied"
        self._persist_tasks()

    def _require_task(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise SsiosError("not_found", "task not found", 404)
        return task


def production_blocked(settings) -> bool:
    return settings.profile == "production" or settings.runtime_backend == "cloud-hypervisor"


def preflight_report(settings) -> dict:
    return CloudHypervisorRuntime(settings).preflight()

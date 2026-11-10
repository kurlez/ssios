"""Trusted control plane: authz, tasks, approvals, memory, and storage."""

from __future__ import annotations

import os
import re
import secrets
import stat
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
from ssios.letta_upstream import UpstreamLettaBridge
from ssios.llm import build_llm, is_template_boilerplate
from ssios.memory_store import MemoryStore, validate_document
from ssios.paths import fixtures_dir
from ssios.runtime import CloudHypervisorRuntime, DevGuardRuntime, MacOSVMRuntime, build_runtime
from ssios.storage import ProjectStorage
from ssios.upstream import describe_memfs
from ssios.util import assert_private, atomic_write, read_json, secure_mkdir, sha256_bytes, write_json

SEEDED_PROJECTS = ("demo-alpha", "demo-beta", "local-summary")
_TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def _optional_topic(value: object) -> str | None:
    if value is None or value == "":
        return None
    topic = str(value)
    if not _TOPIC.fullmatch(topic):
        raise SsiosError("project_invalid", "topic rejected", 400)
    return topic

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
    "topic",
    "diagnostic_path",
    "diagnostic_class",
    "runtime_backend",
    "isolated",
    "boot_ms",
    "error_detail",
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
        self.upstream = UpstreamLettaBridge(settings.state_dir, settings.letta_bin)
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
        self._ensure_memory_repos()
        self._check_perms()
        if self._uses_upstream():
            self.upstream.preflight()

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
        for project_id in SEEDED_PROJECTS:
            if not (self.storage.exports / project_id / "exp-1" / "manifest.json").exists():
                self.storage.seed_export(project_id, "exp-1")
        vault = state / "vault" / "canary.txt"
        if not vault.exists():
            data = (self.fixtures / "vault" / "canary.txt").read_bytes()
            atomic_write(vault, data, 0o600)

    def _ensure_memory_repos(self) -> None:
        if self._uses_upstream():
            return
        for project_id in SEEDED_PROJECTS:
            git_dir = self.memory.root / project_id / "repo" / ".git"
            if git_dir.is_dir():
                continue
            if self._memory_history_required(project_id):
                raise SsiosError(
                    "memory_repo_missing",
                    "canonical memory repository is missing; existing approvals and the audit log were left unchanged",
                    500,
                )
            repo = self.memory.repo(project_id)
            if repo.exists():
                self.memory.repair_uninitialized_repo(project_id)
            else:
                self.memory.seed_project(project_id)

    def _memory_history_required(self, project_id: str) -> bool:
        for task in self.tasks.values():
            if task.get("project_id") == project_id and task.get("memory_revision"):
                return True
        for record in self.approvals.records():
            if record.get("project_id") == project_id and str(record.get("action", "")).startswith("memory."):
                return True
        return False

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
            "memory_mode": self.settings.memory_mode,
        }

    def status(self, token: str) -> dict:
        self.require_principal(token)
        return {
            "version": __version__,
            "profile": self.settings.profile,
            "bind": f"{self.settings.host}:{self.bound_port}",
            "telemetry": False,
            "outbound": "deny",
            "memory": describe_memfs(self.settings.memory_mode),
            "llm": {
                "provider": self.settings.llm_provider,
                "deterministic": bool(getattr(self.llm, "deterministic", False)),
                "loopback_only": True,
                "external_fallback": False,
                "model": self.settings.ollama_model if self.settings.llm_provider == "ollama" else "mock",
            },
            "runtime": {
                "active_backend": self.settings.runtime_backend,
                "isolated": False,
                "execution_implemented": self.settings.runtime_backend == "dev-guard",
                "dev_guard": self.dev.describe(),
                "cloud_hypervisor": self.cloud.preflight(),
                "macos_vm": MacOSVMRuntime(self.settings).preflight() if self.settings.runtime_backend == "macos-vm" else {"backend": "macos-vm", "active": False},
                "macos_vz": build_runtime(self.settings).preflight() if self.settings.runtime_backend == "macos-vz" else {"backend": "macos-vz", "active": False},
            },
            "approvals": "separate-human-token",
            "identity": self.service_identity(),
        }

    def service_identity(self) -> dict:
        return {
            "state_dir": str(self.settings.state_dir),
            "config_path": str(self.settings.config_path or ""),
            "profile": self.settings.profile,
            "memory_mode": self.settings.memory_mode,
            "llm_provider": self.settings.llm_provider,
            "ollama_model": self.settings.ollama_model,
            "ollama_base_url": self.settings.ollama_base_url,
            "llm_timeout_seconds": self.settings.llm_timeout_seconds,
            "max_output_tokens": self.settings.max_output_tokens,
            "num_ctx": self.settings.num_ctx,
            "summary_repair_retries": self.settings.summary_repair_retries,
            "max_note_chars": self.settings.max_note_chars,
            "task_timeout_seconds": self.settings.task_timeout_seconds,
            "letta_bin": self.settings.letta_bin,
            "bind": f"{self.settings.host}:{self.bound_port}",
        }

    def preflight(self, token: str) -> dict:
        self.require_human(token)
        return {
            "active_backend": self.settings.runtime_backend,
            "dev_guard": self.dev.describe(),
            "cloud_hypervisor": self.cloud.preflight(),
        }

    def _guest_runtime(self):
        custom = getattr(self, "guest_runtime", None)
        if custom is not None:
            return custom
        return build_runtime(self.settings)

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

    def topic_notes(self, project_id: str, topic: str) -> list[dict]:
        """Committed note bodies under topics/<topic>, without template boilerplate."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", topic):
            raise SsiosError("project_invalid", "topic rejected", 400)
        prefix = f"topics/{topic}/"
        paths = self.upstream.list_markdown(project_id) if self._uses_upstream() else self.memory.list_markdown(project_id)
        notes = []
        for path in paths:
            if not path.startswith(prefix) or not path.endswith(".md"):
                continue
            name = path.rsplit("/", 1)[-1]
            if name in {"MEMORY.md", "last-summary.md"}:
                continue
            try:
                doc = (
                    self.upstream.read_committed(project_id, path)
                    if self._uses_upstream()
                    else self.memory.read_file(project_id, path, include_untrusted=True)
                )
            except SsiosError:
                continue
            content = str(doc.get("content") or "")
            if is_template_boilerplate(path, content):
                continue
            if len(content.strip()) < 40:
                continue
            notes.append({"path": path, "content": content})
        return notes

    def read_core(self, token: str, project_id: str) -> dict:
        with self.lock:
            principal = self._grant(token, project_id, "allow_memory_read")
            if self._uses_upstream():
                return self.upstream.core(project_id, budget=self.settings.core_token_budget)
            return self.memory.core(
                project_id,
                budget=self.settings.core_token_budget,
                include_untrusted=principal.role == "human",
            )

    def read_memory(self, token: str, project_id: str, path: str) -> dict:
        with self.lock:
            principal = self._grant(token, project_id, "allow_memory_read")
            if self._uses_upstream():
                return self.upstream.read_committed(project_id, path)
            return self.memory.read_file(project_id, path, include_untrusted=principal.role == "human")

    def search_memory(self, token: str, project_id: str, query: str) -> dict:
        with self.lock:
            principal = self._grant(token, project_id, "allow_memory_read")
            if self._uses_upstream():
                result = self.upstream.search(project_id, query)
            else:
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
            if self.settings.runtime_backend != "dev-guard":
                runtime = self._guest_runtime()
                if not runtime.can_launch():
                    raise SsiosError(
                        "microvm_fail_closed",
                        "microVM backend cannot start a guest; the task was not run on the host",
                        503,
                    )
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
                "topic": _optional_topic(body.get("topic")),
                "diagnostic_path": None,
                "diagnostic_class": None,
                "runtime_backend": self.settings.runtime_backend,
                "isolated": False,
                "boot_ms": None,
                "error_detail": None,
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
            task_id = principal.grant.task_id if principal.grant else "human"
            expected = str(body.get("expected_revision", ""))
            if self._uses_upstream():
                proposal = self.upstream.stage_proposal(project_id, expected, files, task_id=task_id)
            else:
                proposal = self.memory.propose(project_id, expected, files, task_id=task_id)
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
            task_id = principal.grant.task_id if principal.grant else "human"
            if self._uses_upstream():
                proposal = self.upstream.stage_forget(
                    project_id,
                    str(body.get("expected_revision", "")),
                    str(body.get("path", "")),
                    task_id=task_id,
                )
            else:
                proposal = self.memory.propose_forget(
                    project_id,
                    str(body.get("expected_revision", "")),
                    str(body.get("path", "")),
                    task_id=task_id,
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

    def reissue_expired(self, token: str, task_id: str) -> dict:
        """Mint new approvals for expired, unapplied actions. Does not approve them."""
        with self.lock:
            self.require_human(token)
            task = self._require_task(task_id)
            if task["status"] != "awaiting_approval":
                raise SsiosError("task_closed", "task is not awaiting approval", 409)
            issued: list[str] = []
            for approval_id in list(task["approval_ids"]):
                current = self.approvals.get(approval_id)
                if current["status"] == "pending" and self.now() >= float(current["expires_at"]):
                    self.approvals.mark(approval_id, status="expired", decision=None)
                    current = self.approvals.get(approval_id)
                if current["status"] != "expired":
                    continue
                if self._open_successor(task, approval_id):
                    continue
                if self._effect_already_applied(current):
                    continue
                issued.append(self._mint_replacement(task, current))
            self._refresh_task(task_id)
            public = self.get_task(token, task_id)
            public["issued"] = issued
            return public

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
            public["files"] = self._preview_files(record)
            return public

    def decide(self, token: str, approval_id: str, body: dict) -> dict:
        with self.lock:
            self.require_human(token, approval=True)
            record = self.approvals.get(approval_id)
            if record["status"] == "expired" or (
                record["status"] == "pending" and self.now() >= float(record["expires_at"])
            ):
                if record["status"] == "pending":
                    self.approvals.mark(approval_id, status="expired", decision=None)
                self.audit.write(
                    ts=self.now(),
                    actor_role="human",
                    action="approval.decide",
                    result="denied",
                    approval_id=approval_id,
                    error_code="approval_expired",
                    payload_sha256=record["payload_sha256"],
                    task_id=record["task_id"],
                    project_id=record["project_id"],
                )
                raise SsiosError("approval_expired", "approval expired", 409)
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
            decision = body.get("decision")
            if decision not in ("approve", "deny"):
                raise SsiosError("approval_invalid", "decision rejected", 400)
            task = self.tasks.get(record["task_id"])
            if (
                decision != "deny"
                and task is not None
                and task["status"] in ("cancelled", "timeout", "failed", "rejected")
            ):
                raise SsiosError("task_closed", "task is closed", 409)
            if decision == "deny":
                self.approvals.mark(approval_id, status="denied", decision="deny")
                if record.get("proposal_id"):
                    self._mark_proposal(record["proposal_id"], "rejected")
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
                        self._mark_proposal(record["proposal_id"], "conflict")
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
        limit = self.settings.task_timeout_seconds
        if self.settings.runtime_backend != "dev-guard":
            limit += self.settings.guest_prepare_timeout_seconds
        deadline = time.monotonic() + limit
        self._arm(cancel_event, deadline)
        try:
            if self.settings.runtime_backend == "dev-guard":
                result = self.runner.run(task, token, cancel_event, deadline)
            else:
                from ssios.vmguest import execute_guest

                result = execute_guest(self, task, token, cancel_event, deadline)
            self._finish(task_id, token, result)
        except FlowInterrupt:
            kind = "cancelled" if self.user_cancelled(task_id) else "timeout"
            self._close_task(task_id, kind, kind)
        except SsiosError as exc:
            self._save_diagnostic(task_id, exc)
            self._close_task(task_id, "failed", exc.code)
        except Exception as exc:
            try:
                self._save_internal_diagnostic(task_id, exc)
            except Exception:
                traceback.print_exc()
                self._set_error_detail(task_id, f"{type(exc).__name__}: {exc}"[:500])
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
            if self._uses_upstream():
                try:
                    topic = self.upstream.read_committed(task["project_id"], "topics/fixtures/MEMORY.md")
                    index = topic["content"]
                except SsiosError as exc:
                    if exc.code != "not_found":
                        raise
                    index = "# Fixtures\n"
                revision = self.upstream.head(task["project_id"])
            else:
                topic = self.memory.read_file(task["project_id"], "topics/fixtures/MEMORY.md", include_untrusted=False)
                index = topic["content"]
                revision = self.memory.head(task["project_id"])
            if "last-summary.md" not in index:
                index = index.rstrip() + "\n- last-summary.md — approved task summary\n"
            summary_doc = (
                "---\n"
                "name: last-summary\n"
                'description: "Approved synthetic task summary."\n'
                "---\n\n"
                f"{result['final_text']}\n"
            )
            files = [
                {"path": "topics/fixtures/last-summary.md", "content": summary_doc},
                {"path": "topics/fixtures/MEMORY.md", "content": index},
            ]
            if self._uses_upstream():
                proposal = self.upstream.stage_proposal(
                    task["project_id"],
                    revision,
                    files,
                    task_id=task_id,
                )
            else:
                proposal = self.memory.propose(
                    task["project_id"],
                    revision,
                    files,
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
            task["runtime_backend"] = result.get("runtime_backend", task.get("runtime_backend"))
            task["isolated"] = bool(result.get("isolated", task.get("isolated")))
            task["boot_ms"] = result.get("boot_ms", task.get("boot_ms"))
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

    def _save_diagnostic(self, task_id: str, exc: SsiosError) -> None:
        payload = getattr(exc, "diagnostic", None)
        if exc.code in {
            "socket_path_too_long",
            "guest_vsock_unavailable",
            "guest_virtiofs_unavailable",
            "guest_nic_present",
        }:
            commands = []
            if isinstance(payload, dict):
                commands = payload.get("commands") or []
            self._write_runtime_diagnostic(task_id, exc.code, exc.detail, commands if isinstance(commands, list) else [])
            return
        if not isinstance(payload, dict):
            self._set_error_detail(task_id, exc.detail)
            return
        if exc.code == "microvm_fail_closed":
            self._save_microvm_diagnostic(task_id, exc, payload)
            return
        if exc.code not in ("model_output_invalid", "model_output_low_quality"):
            self._set_error_detail(task_id, exc.detail)
            return
        if not re.fullmatch(r"task-[0-9a-f]{16}", task_id):
            return
        directory = self.settings.state_dir / "diagnostics"
        secure_mkdir(directory)
        path = directory / f"{task_id}.json"
        body = {
            "task_id": task_id,
            "classification": str(payload.get("classification") or "not_json"),
            "content": str(payload.get("content") or ""),
            "thinking": str(payload.get("thinking") or ""),
        }
        write_json(path, body, 0o600)
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            task["diagnostic_path"] = str(path)
            task["diagnostic_class"] = body["classification"]
            self._persist_tasks()

    def _save_microvm_diagnostic(self, task_id: str, exc: SsiosError, payload: dict) -> None:
        if not re.fullmatch(r"task-[0-9a-f]{16}", task_id):
            return
        from ssios.vmguest import redact_text

        secrets = []
        for name in ("human", "worker"):
            try:
                secret = self.auth.read_secret(name)
            except (SsiosError, OSError):
                secret = ""
            if secret:
                secrets.append(secret)
        commands = []
        for item in payload.get("commands") or []:
            if not isinstance(item, dict):
                continue
            commands.append(
                {
                    "argv": [redact_text(str(part), secrets) for part in item.get("argv") or []],
                    "exit_code": item.get("exit_code"),
                    "stdout_tail": redact_text(str(item.get("stdout_tail") or ""), secrets),
                    "stderr_tail": redact_text(str(item.get("stderr_tail") or ""), secrets),
                }
            )
        detail = redact_text(str(payload.get("detail") or exc.detail), secrets)[:500]
        self._write_runtime_diagnostic(task_id, "microvm", detail, commands, secrets)

    def _save_internal_diagnostic(self, task_id: str, exc: BaseException) -> None:
        secrets = self._task_secrets()
        from ssios.vmguest import redact_text

        detail = redact_text(f"{type(exc).__name__}: {exc}", secrets)[:500]
        trace = redact_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:], secrets)
        self._write_runtime_diagnostic(task_id, "internal", detail, [], secrets, trace=trace)

    def _task_secrets(self) -> list[str]:
        found = []
        for name in ("human", "worker"):
            try:
                secret = self.auth.read_secret(name)
            except (SsiosError, OSError):
                secret = ""
            if secret:
                found.append(secret)
        return found

    def _write_runtime_diagnostic(
        self,
        task_id: str,
        classification: str,
        detail: str,
        commands: list,
        secrets: list[str] | None = None,
        trace: str = "",
    ) -> None:
        if not re.fullmatch(r"task-[0-9a-f]{16}", task_id):
            self._set_error_detail(task_id, detail)
            return
        from ssios.vmguest import redact_text

        hidden = self._task_secrets() if secrets is None else secrets
        serial_path, serial_tail = self._serial_evidence(task_id, hidden)
        directory = self.settings.state_dir / "diagnostics"
        secure_mkdir(directory)
        path = directory / f"{task_id}.json"
        body = {
            "task_id": task_id,
            "classification": classification,
            "detail": redact_text(detail, hidden)[:500],
            "commands": commands,
            "serial_log": serial_path,
            "serial_tail": serial_tail,
        }
        if trace:
            body["traceback"] = trace
        write_json(path, body, 0o600)
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            task["diagnostic_path"] = str(path)
            task["diagnostic_class"] = classification
            task["error_detail"] = body["detail"]
            self._persist_tasks()

    def _serial_evidence(self, task_id: str, secrets: list[str]) -> tuple[str, str]:
        from ssios.vmguest import redact_text

        path = self.settings.state_dir / "storage" / "tasks" / task_id / "guest" / "serial.log"
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return "", ""
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return "", ""
            if info.st_size > 2000:
                os.lseek(fd, -2000, os.SEEK_END)
            text = os.read(fd, 2000).decode("utf-8", "replace")
        finally:
            os.close(fd)
        return str(path), redact_text(text, secrets)

    def _set_error_detail(self, task_id: str, detail: str) -> None:
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            task["error_detail"] = str(detail)[:500]
            self._persist_tasks()

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
            if self._uses_upstream():
                revision = self.upstream.apply(
                    record["proposal_id"],
                    record["expected_revision"],
                    record["approval_id"],
                    record["payload_sha256"],
                    approved=True,
                )
            else:
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
        if self._uses_upstream():
            proposal = self.upstream.load_proposal(record["proposal_id"])
        else:
            proposal = self.memory.load_proposal(record["proposal_id"])
        if record["action"] == "memory.forget":
            return proposal["path"]
        return "\n\n".join(item["content"] for item in proposal["files"])

    def _preview_files(self, record: dict) -> list[dict]:
        if record["action"] == "storage.publish":
            return [{"path": "summary.md", "content": self._preview_text(record)}]
        if record["action"] == "memory.forget":
            proposal = self._load_proposal(record["proposal_id"])
            self._check_proposal_hash(proposal, record["payload_sha256"])
            return [{"path": str(proposal.get("path", "")), "content": ""}]
        if record["action"] != "memory.commit":
            return []
        proposal = self._load_proposal(record["proposal_id"])
        self._check_proposal_hash(proposal, record["payload_sha256"])
        files = []
        for item in proposal.get("files") or []:
            files.append({"path": str(item.get("path", "")), "content": str(item.get("content", ""))})
        return files

    def _check_proposal_hash(self, proposal: dict, payload_sha256: str) -> None:
        if MemoryStore.proposal_hash(proposal) != payload_sha256:
            raise SsiosError("payload_mismatch", "proposal bytes do not match the approval", 409)

    def _refresh_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task is None or not task["approval_ids"]:
            return
        records = [self.approvals.get(item) for item in task["approval_ids"]]
        if any(item["status"] == "pending" for item in records):
            if task["status"] not in ("cancelled", "timeout", "failed", "rejected"):
                task["status"] = "awaiting_approval"
        elif any(item["status"] == "denied" for item in records):
            task["status"] = "rejected"
            task["error_code"] = "denied"
        elif any(item["status"] == "conflict" for item in records):
            task["status"] = "failed"
            task["error_code"] = "cas_conflict"
        elif any(item["status"] == "tampered" for item in records):
            task["status"] = "failed"
            task["error_code"] = "payload_mismatch"
        elif records and all(self._approval_satisfied(task, item) for item in records):
            task["status"] = "committed"
            task["error_code"] = None
        self._persist_tasks()

    def _require_task(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise SsiosError("not_found", "task not found", 404)
        return task

    def _uses_upstream(self) -> bool:
        return self.settings.memory_mode == "letta-memfs-upstream"

    def _open_successor(self, task: dict, approval_id: str) -> bool:
        for other in self.approvals.records():
            if other.get("replaces") != approval_id or other.get("task_id") != task["id"]:
                continue
            if other["status"] not in ("pending", "consumed"):
                continue
            if other["approval_id"] not in task["approval_ids"]:
                task["approval_ids"].append(other["approval_id"])
            return True
        return False

    def _effect_already_applied(self, record: dict) -> bool:
        if record["action"] == "storage.publish":
            return self._published_matches(record["project_id"], record["payload_sha256"])
        if record["action"] not in ("memory.commit", "memory.forget"):
            return False
        proposal = self._load_proposal(record["proposal_id"])
        if record["action"] == "memory.forget":
            try:
                current = self._read_memory_file(record["project_id"], proposal["path"])
            except SsiosError as exc:
                return exc.code == "not_found"
            return bool(current.get("tombstone"))
        return all(
            self._memory_file_equals(record["project_id"], item["path"], item["content"])
            for item in proposal.get("files") or []
        )

    def _published_matches(self, project_id: str, payload_sha256: str) -> bool:
        head = self.storage.published_head(project_id)
        if head in ("", "none"):
            return False
        try:
            body = self.storage.read_published(project_id, head)
        except SsiosError:
            return False
        return body.get("sha256") == payload_sha256

    def _memory_file_equals(self, project_id: str, path: str, content: str) -> bool:
        try:
            current = self._read_memory_file(project_id, path)
        except SsiosError as exc:
            if exc.code == "not_found":
                return False
            raise
        return current.get("content") == content

    def _read_memory_file(self, project_id: str, path: str) -> dict:
        if self._uses_upstream():
            return self.upstream.read_committed(project_id, path)
        return self.memory.read_file(project_id, path, include_untrusted=True)

    def _load_proposal(self, proposal_id: str) -> dict:
        if self._uses_upstream():
            return self.upstream.load_proposal(proposal_id)
        return self.memory.load_proposal(proposal_id)

    def _mint_replacement(self, task: dict, expired: dict) -> str:
        if expired["action"] == "storage.publish":
            staged = self.storage.staged_bytes(task["id"])
            if sha256_bytes(staged) != expired["payload_sha256"]:
                raise SsiosError("payload_mismatch", "staged bytes do not match the approval", 409)
            created = self._approval(
                action="storage.publish",
                project_id=expired["project_id"],
                task_id=task["id"],
                payload_sha256=expired["payload_sha256"],
                expected_revision=self.storage.published_head(expired["project_id"]),
                proposal_id=None,
            )
        elif expired["action"] in ("memory.commit", "memory.forget"):
            proposal = self._load_proposal(expired["proposal_id"])
            head = self.upstream.head(expired["project_id"]) if self._uses_upstream() else self.memory.head(expired["project_id"])
            task_label = task["id"]
            if expired["action"] == "memory.forget":
                staged = (
                    self.upstream.stage_forget(expired["project_id"], head, proposal["path"], task_id=task_label)
                    if self._uses_upstream()
                    else self.memory.propose_forget(expired["project_id"], head, proposal["path"], task_id=task_label)
                )
            else:
                staged = (
                    self.upstream.stage_proposal(expired["project_id"], head, proposal["files"], task_id=task_label)
                    if self._uses_upstream()
                    else self.memory.propose(expired["project_id"], head, proposal["files"], task_id=task_label)
                )
            self._mark_proposal(expired["proposal_id"], "superseded")
            created = self._approval(
                action=expired["action"],
                project_id=expired["project_id"],
                task_id=task["id"],
                payload_sha256=staged["payload_sha256"],
                expected_revision=head,
                proposal_id=staged["id"],
            )
        else:
            raise SsiosError("approval_invalid", "action rejected", 400)
        stored = self.approvals.get(created["id"])
        stored["replaces"] = expired["approval_id"]
        self.approvals.save()
        task["approval_ids"].append(created["id"])
        self.audit.write(
            ts=self.now(),
            actor_role="human",
            action="approval.reissue",
            result="staged",
            task_id=task["id"],
            project_id=expired["project_id"],
            approval_id=created["id"],
            replaces=expired["approval_id"],
            payload_sha256=created["payload_sha256"],
            revision=created["expected_revision"],
        )
        return created["id"]

    def _approval_satisfied(self, task: dict, record: dict) -> bool:
        if record["status"] == "consumed" and record["decision"] == "approve":
            return True
        if record["status"] != "expired":
            return False
        for other in self.approvals.records():
            if (
                other.get("replaces") == record["approval_id"]
                and other.get("task_id") == task["id"]
                and other["status"] == "consumed"
                and other.get("decision") == "approve"
            ):
                return True
        return self._effect_already_applied(record)

    def _mark_proposal(self, proposal_id: str, status: str) -> None:
        if self._uses_upstream():
            self.upstream.mark_proposal(proposal_id, status)
        else:
            self.memory.mark_proposal(proposal_id, status)


def production_blocked(settings) -> bool:
    return settings.profile == "production" or settings.runtime_backend == "cloud-hypervisor"


def preflight_report(settings) -> dict:
    return CloudHypervisorRuntime(settings).preflight()

"""Optional Letta Code local MemFS bridge.

This process is outside the trusted permission broker. It may run only the
documented JSON subcommands of a pinned Letta Code CLI, plus git inside the
memory directory that CLI created. It never starts `letta -p` and never passes
`--yolo`. If the CLI is missing or the version differs, callers must fail
closed. There is no fallback to the compat store.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from ssios.errors import SsiosError
from ssios.gitio import GitRunner
from ssios.memory_store import MemoryStore, validate_document
from ssios.util import canonical_json, sha256_bytes, validate_rel_parts, write_json, write_nofollow

PINNED_VERSION = "0.34.45"
PINNED_PACKAGE = "@letta-ai/letta-code"
PINNED_INTEGRITY = "sha512-IhB4vDuNebew2bKbAGYJ3zROPG6kLD2rPhMHXOSSxjExDFCTLAN74d5/4Sn7K8d3yV8n2mZT5uWVcA4FbN+yJw=="
AGENT_ID_RE = re.compile(r"^agent-local-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
FORBIDDEN_ARGV = {
    "-p",
    "--yolo",
    "--permission-mode",
    "--tools",
    "--allowedTools",
    "--disallowedTools",
    "--disable-memory-guard",
    "--ephemeral",
}


def validate_letta_argv(argv: list[str]) -> None:
    if not argv or not isinstance(argv[0], str):
        raise SsiosError("upstream_rejected", "letta command rejected", 500)
    for part in argv:
        if not isinstance(part, str) or "\x00" in part or "\n" in part:
            raise SsiosError("upstream_rejected", "letta command rejected", 500)
        if part in FORBIDDEN_ARGV or part.startswith("-p"):
            raise SsiosError("upstream_rejected", "letta prompt and unrestricted modes are rejected", 403)


class UpstreamLettaBridge:
    def __init__(self, state_dir: Path, letta_bin: str) -> None:
        self.state_dir = state_dir if state_dir.is_absolute() else (Path.cwd() / state_dir).resolve()
        self.root = self.state_dir / "upstream-letta"
        self.backend_dir = self.root / "backend"
        self.home_dir = self.root / "empty-home"
        self.work_dir = self.root / "empty-cwd"
        self.hooks_dir = self.root / "git-hooks"
        self.empty_gitconfig = self.root / "empty-gitconfig"
        self.proposals = self.root / "proposals"
        self.exports = self.root / "exports"
        self.map_path = self.root / "projects.json"
        self.letta_bin = letta_bin
        self.git_runner = GitRunner(self.hooks_dir, self.empty_gitconfig)
        self._projects: dict[str, str] = {}

    def ensure_layout(self) -> None:
        for path in (self.root, self.backend_dir, self.home_dir, self.work_dir, self.hooks_dir, self.proposals, self.exports):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        if not self.empty_gitconfig.exists():
            self.empty_gitconfig.write_bytes(b"")
            os.chmod(self.empty_gitconfig, 0o600)
        if self.map_path.exists():
            loaded = json.loads(self.map_path.read_text(encoding="utf-8"))
            self._projects = {str(key): str(value) for key, value in loaded.items()}

    def preflight(self) -> dict:
        self.ensure_layout()
        binary = self._resolve_bin()
        completed = self._run([binary, "--version"], timeout=20)
        text = (completed.stdout or "") + (completed.stderr or "")
        if PINNED_VERSION not in text:
            raise SsiosError(
                "upstream_unavailable",
                f"letta version is not the pinned {PINNED_VERSION}",
                503,
            )
        return {
            "ok": True,
            "mode": "letta-memfs-upstream",
            "package": PINNED_PACKAGE,
            "version": PINNED_VERSION,
            "integrity": PINNED_INTEGRITY,
            "binary": binary,
            "harness_prompt": False,
            "fallback": "none",
            "live_integration": True,
        }

    def ensure_agent(self, project_id: str) -> str:
        self._project(project_id)
        self.ensure_layout()
        existing = self._projects.get(project_id)
        if existing:
            self._memory_dir(existing)
            return existing
        completed = self._run(
            [
                self._resolve_bin(),
                "--backend",
                "local",
                "agents",
                "create",
                "--name",
                f"ssios-{project_id}",
                "--description",
                "Synthetic SSIOS project fixture. No personal data.",
                "--personality",
                "blank",
            ],
            timeout=60,
        )
        agent_id = _agent_id_from_create(completed.stdout)
        self._memory_dir(agent_id)
        self._projects[project_id] = agent_id
        write_json(self.map_path, self._projects)
        return agent_id

    def head(self, project_id: str) -> str:
        agent_id = self.ensure_agent(project_id)
        return self._git(self._memory_dir(agent_id), ["rev-parse", "HEAD"]).stdout.decode().strip()

    def list_markdown(self, project_id: str) -> list[str]:
        agent_id = self.ensure_agent(project_id)
        completed = self._git(self._memory_dir(agent_id), ["ls-tree", "-r", "--name-only", "HEAD"], check=False)
        if completed.returncode != 0:
            return []
        return [line.decode("utf-8") for line in completed.stdout.splitlines() if line.endswith(b".md")]

    def core(self, project_id: str, *, budget: int) -> dict:
        agent_id = self.ensure_agent(project_id)
        tokens = self.tokens(agent_id)
        exported = self.export_tree(agent_id)
        revision = self.head(project_id)
        selected = []
        used = 0
        entries = []
        for item in tokens["files"]:
            path = str(item["path"])
            validate_rel_parts(path)
            file_path = exported / path
            if file_path.is_symlink() or not file_path.is_file():
                raise SsiosError("upstream_invalid", "upstream core file is not a regular file", 500)
            text = file_path.read_text(encoding="utf-8")
            entries.append({"path": path, "content": text, "upstream_tokens": item.get("tokens")})
        truncated = False
        for item in entries:
            remaining = budget - used
            cost = max(1, (len(item["content"]) + 3) // 4)
            if remaining <= 0:
                truncated = True
                break
            if cost > remaining:
                truncated = True
                shortened = {**item, "content": item["content"][: remaining * 4], "truncated": True}
                selected.append(shortened)
                used += max(1, (len(shortened["content"]) + 3) // 4)
                break
            selected.append(item)
            used += cost
        selected_paths = {item["path"] for item in selected}
        omitted = [item["path"] for item in entries if item["path"] not in selected_paths]
        topics = _topic_indexes(exported, {item["path"] for item in entries})
        return {
            "project_id": project_id,
            "revision": revision,
            "mode": "letta-memfs-upstream",
            "source": "letta-memory-tokens+export",
            "agent_id": agent_id,
            "upstream_token_total": tokens.get("total_tokens"),
            "token_estimate": used,
            "budget": budget,
            "truncated": truncated or bool(omitted),
            "omitted": omitted,
            "topics": topics,
            "files": selected,
        }

    def read_committed(self, project_id: str, rel: str) -> dict:
        agent_id = self.ensure_agent(project_id)
        validate_rel_parts(rel)
        memory = self._memory_dir(agent_id)
        shown = self._git(memory, ["show", f"HEAD:{rel}"], check=False)
        if shown.returncode != 0:
            raise SsiosError("not_found", "not found", 404)
        exported = self.export_tree(agent_id)
        exported_file = exported / rel
        if exported_file.is_symlink() or not exported_file.is_file():
            raise SsiosError("upstream_invalid", "export does not contain the committed file", 500)
        if exported_file.read_bytes() != shown.stdout:
            raise SsiosError("upstream_invalid", "export bytes do not match git HEAD", 500)
        tokens = self.tokens(agent_id)
        core_paths = {str(item["path"]) for item in tokens["files"]}
        return {
            "project_id": project_id,
            "path": rel,
            "revision": self.head(project_id),
            "tombstone": False,
            "trust": "upstream-letta",
            "core": rel in core_paths,
            "content": shown.stdout.decode("utf-8"),
            "source": "letta-memory-export",
        }

    def search(self, project_id: str, query: str) -> dict:
        if not query or len(query) > 200:
            raise SsiosError("search_invalid", "query rejected", 400)
        agent_id = self.ensure_agent(project_id)
        exported = self.export_tree(agent_id)
        needle = query.casefold()
        hits = []
        for path in sorted(exported.rglob("*")):
            if ".git" in path.parts or path.is_symlink() or not path.is_file():
                continue
            if path.suffix != ".md":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            at = text.casefold().find(needle)
            if at < 0:
                continue
            hits.append({"path": path.relative_to(exported).as_posix(), "snippet": text[max(0, at - 40) : at + 120]})
            if len(hits) >= 20:
                break
        return {
            "project_id": project_id,
            "revision": self.head(project_id),
            "search_mode": "keyword-over-letta-memory-export",
            "query_sha256": sha256_bytes(query.encode("utf-8")),
            "hits": hits,
        }

    def stage_proposal(self, project_id: str, expected_revision: str, files: list[dict], *, task_id: str) -> dict:
        if self.head(project_id) != expected_revision:
            raise SsiosError("cas_conflict", "memory revision changed", 409)
        cleaned = []
        for item in files:
            path = str(item.get("path", ""))
            content = str(item.get("content", ""))
            validate_document(path, content)
            cleaned.append({"path": path, "content": content})
        payload = {"expected_revision": expected_revision, "files": cleaned, "project_id": project_id}
        proposal_id = "prop-" + secrets.token_hex(8)
        record = {
            "id": proposal_id,
            "project_id": project_id,
            "task_id": task_id,
            "kind": "commit",
            "expected_revision": expected_revision,
            "files": cleaned,
            "payload_sha256": sha256_bytes(canonical_json(payload)),
            "trust": "untrusted",
            "status": "pending",
        }
        record["payload_sha256"] = MemoryStore.proposal_hash(record)
        write_json(self.proposals / f"{proposal_id}.json", record)
        return record

    def stage_forget(self, project_id: str, expected_revision: str, path: str, *, task_id: str) -> dict:
        current = self.read_committed(project_id, path)
        if current.get("tombstone"):
            raise SsiosError("already_forgotten", "memory file is already a tombstone", 409)
        if self.head(project_id) != expected_revision:
            raise SsiosError("cas_conflict", "memory revision changed", 409)
        proposal_id = "prop-" + secrets.token_hex(8)
        record = {
            "id": proposal_id,
            "project_id": project_id,
            "task_id": task_id,
            "kind": "forget",
            "expected_revision": expected_revision,
            "path": path,
            "payload_sha256": "",
            "trust": "untrusted",
            "status": "pending",
        }
        record["payload_sha256"] = MemoryStore.proposal_hash(record)
        write_json(self.proposals / f"{proposal_id}.json", record)
        return record

    def mark_proposal(self, proposal_id: str, status: str) -> None:
        proposal = self.load_proposal(proposal_id)
        proposal["status"] = status
        write_json(self.proposals / f"{proposal_id}.json", proposal)

    def load_proposal(self, proposal_id: str) -> dict:
        if not re.fullmatch(r"prop-[0-9a-f]{16}", proposal_id):
            raise SsiosError("not_found", "proposal not found", 404)
        path = self.proposals / f"{proposal_id}.json"
        if not path.is_file():
            raise SsiosError("not_found", "proposal not found", 404)
        return json.loads(path.read_text(encoding="utf-8"))

    def apply(self, proposal_id: str, expected_revision: str, approval_id: str, payload_sha256: str, *, approved: bool) -> str:
        if approved is not True:
            raise SsiosError("upstream_rejected", "upstream memory commit requires human approval", 403)
        proposal = self.load_proposal(proposal_id)
        if proposal["status"] != "pending":
            raise SsiosError("proposal_closed", "proposal is not pending", 409)
        if MemoryStore.proposal_hash(proposal) != payload_sha256:
            raise SsiosError("payload_mismatch", "proposal bytes do not match the approval", 409)
        project_id = proposal["project_id"]
        agent_id = self.ensure_agent(project_id)
        memory = self._memory_dir(agent_id)
        head = self._git(memory, ["rev-parse", "HEAD"]).stdout.decode().strip()
        if head != expected_revision or proposal["expected_revision"] != expected_revision:
            raise SsiosError("cas_conflict", "memory revision changed", 409)
        if proposal["kind"] == "commit":
            paths = []
            for item in proposal["files"]:
                validate_document(item["path"], item["content"])
                write_nofollow(memory, item["path"], item["content"].encode("utf-8"))
                paths.append(item["path"])
            self.git_runner.commit_paths(memory, paths, f"ssios: approved upstream memory {proposal_id}")
        elif proposal["kind"] == "forget":
            rel = proposal["path"]
            validate_rel_parts(rel)
            self.git_runner.remove_path(memory, rel, f"ssios: forget upstream memory {proposal_id}")
        else:
            raise SsiosError("memory_invalid", "unknown proposal kind", 400)
        status = self.status(agent_id)
        if status.get("dirty"):
            raise SsiosError("upstream_invalid", "letta memory status is still dirty after commit", 500)
        revision = self._git(memory, ["rev-parse", "HEAD"]).stdout.decode().strip()
        if proposal["kind"] == "commit":
            exported = self.export_tree(agent_id)
            for item in proposal["files"]:
                exported_file = exported / item["path"]
                if exported_file.read_text(encoding="utf-8") != item["content"]:
                    raise SsiosError("upstream_invalid", "letta memory export does not match the approved bytes", 500)
        proposal["status"] = "committed"
        proposal["committed_revision"] = revision
        proposal["approval_id"] = approval_id
        write_json(self.proposals / f"{proposal_id}.json", proposal)
        return revision

    def tokens(self, agent_id: str) -> dict:
        completed = self._run(
            [self._resolve_bin(), "--backend", "local", "memory", "tokens", "--agent", agent_id, "--format", "json"],
            timeout=30,
        )
        payload = _json_object(completed.stdout)
        if not isinstance(payload.get("files"), list):
            raise SsiosError("upstream_invalid", "letta memory tokens did not return files", 500)
        return payload

    def status(self, agent_id: str) -> dict:
        completed = self._run(
            [self._resolve_bin(), "--backend", "local", "memory", "status", "--agent", agent_id],
            timeout=30,
            allow_returncodes=(0, 2),
        )
        return _json_object(completed.stdout)

    def export_tree(self, agent_id: str) -> Path:
        out = self.exports / agent_id / secrets.token_hex(4)
        completed = self._run(
            [self._resolve_bin(), "--backend", "local", "memory", "export", "--agent", agent_id, "--out", str(out)],
            timeout=30,
        )
        payload = _json_object(completed.stdout)
        exported_from = Path(str(payload.get("exportedFrom", "")))
        if exported_from.resolve() != self._memory_dir(agent_id):
            raise SsiosError("upstream_invalid", "letta export source is not the agent memory directory", 500)
        if Path(str(payload.get("exportedTo", ""))).resolve() != out.resolve():
            raise SsiosError("upstream_invalid", "letta export destination mismatch", 500)
        return out

    def _memory_dir(self, agent_id: str) -> Path:
        if not AGENT_ID_RE.fullmatch(agent_id):
            raise SsiosError("upstream_invalid", "agent id rejected", 500)
        path = (self.backend_dir / "memfs" / agent_id / "memory").resolve()
        root = self.backend_dir.resolve()
        if root not in path.parents:
            raise SsiosError("upstream_invalid", "memory path escaped the backend directory", 500)
        if not (path / ".git").is_dir():
            raise SsiosError("upstream_unavailable", "letta did not create a local MemFS git repository", 503)
        return path

    def _resolve_bin(self) -> str:
        candidate = self.letta_bin or os.environ.get("SSIOS_LETTA_BIN", "") or shutil.which("letta") or ""
        if not candidate:
            raise SsiosError("upstream_unavailable", "pinned letta CLI is not installed", 503)
        path = Path(candidate)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise SsiosError("upstream_unavailable", "pinned letta CLI is not executable", 503)
        return str(path)

    def _run(self, argv: list[str], *, timeout: float, allow_returncodes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
        validate_letta_argv(argv)
        self.ensure_layout()
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            cwd=str(self.work_dir),
            env=self._env(),
        )
        if completed.returncode not in allow_returncodes:
            detail = (completed.stderr or completed.stdout or "letta command failed").strip().splitlines()
            raise SsiosError("upstream_unavailable", detail[0][:240] if detail else "letta command failed", 503)
        return completed

    def _git(self, memory: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        allowed = memory.resolve()
        try:
            relative = allowed.relative_to(self.backend_dir.resolve())
        except ValueError as exc:
            raise SsiosError("upstream_rejected", "git is confined to the Letta memory directory", 403) from exc
        if (
            len(relative.parts) != 3
            or relative.parts[0] != "memfs"
            or relative.parts[2] != "memory"
            or not AGENT_ID_RE.fullmatch(relative.parts[1])
        ):
            raise SsiosError("upstream_rejected", "git is confined to the Letta memory directory", 403)
        return self.git_runner.run(allowed, args, check=check)

    def _env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home_dir),
            "LANG": "C.UTF-8",
            "LETTA_LOCAL_BACKEND_DIR": str(self.backend_dir),
            "LETTA_CODE_TELEM": "0",
            "DISABLE_AUTOUPDATER": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.empty_gitconfig),
            "GIT_TERMINAL_PROMPT": "0",
        }

    @staticmethod
    def _project(project_id: str) -> None:
        if not PROJECT_RE.fullmatch(project_id):
            raise SsiosError("project_invalid", "project id rejected", 400)


def _agent_id_from_create(stdout: str) -> str:
    payload = _json_object(stdout)
    agent_id = str(payload.get("id", ""))
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise SsiosError("upstream_invalid", "letta agents create did not return a local agent id", 500)
    return agent_id


def _json_object(stdout: str) -> dict:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end <= start:
        raise SsiosError("upstream_invalid", "letta did not return JSON", 500)
    payload = json.loads(stdout[start : end + 1])
    if not isinstance(payload, dict):
        raise SsiosError("upstream_invalid", "letta JSON was not an object", 500)
    return payload


def _topic_indexes(root: Path, core_paths: set[str]) -> list[str]:
    found = []
    for path in root.rglob("MEMORY.md"):
        if ".git" in path.parts or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in core_paths or rel == "MEMORY.md":
            continue
        found.append(rel)
    return sorted(found)

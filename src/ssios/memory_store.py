"""Letta MemFS local compatibility store.

This is a Git-backed Markdown checkout with Letta's documented layout:
root Markdown is core memory, nested MEMORY.md directories stay on demand,
and approved edits become commits. It is not the Letta Code process and it
does not call Letta cloud APIs.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from ssios.errors import SsiosError
from ssios.gitio import GitRunner
from ssios.util import (
    canonical_json,
    copy_tree_regular,
    read_json,
    sha256_bytes,
    token_estimate,
    validate_rel_parts,
    write_json,
    write_nofollow,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
TRUSTED = {"synthetic_seed", "approved"}


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    lines = text.split("\n")
    meta: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            body = "\n".join(lines[index + 1 :])
            if body.startswith("\n"):
                body = body[1:]
            return meta, body
        if not line.strip():
            continue
        if ":" not in line:
            raise SsiosError("frontmatter_invalid", "frontmatter is invalid", 400)
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        meta[key.strip()] = value
    raise SsiosError("frontmatter_invalid", "frontmatter is unterminated", 400)


def validate_document(path: str, content: str) -> None:
    parts = validate_rel_parts(path)
    if not parts[-1].endswith(".md"):
        raise SsiosError("memory_invalid", "memory files must be markdown", 400)
    if len(content.encode("utf-8")) > 65536:
        raise SsiosError("memory_invalid", "memory file is too large", 400)
    if parts[-1] == "MEMORY.md":
        if content.startswith("---\n"):
            raise SsiosError("memory_invalid", "MEMORY.md index has no frontmatter", 400)
        return
    meta, _body = parse_frontmatter(content)
    if "name" not in meta or "description" not in meta:
        raise SsiosError("memory_invalid", "memory file requires name and description", 400)


class MemoryStore:
    def __init__(self, state_dir: Path, git: GitRunner, fixtures: Path) -> None:
        self.root = state_dir / "memory"
        self.fixtures = fixtures / "memory"
        self.git = git
        self.proposals = self.root / "proposals"

    def repo(self, project_id: str) -> Path:
        self._project(project_id)
        return self.root / project_id / "repo"

    def provenance_path(self, project_id: str) -> Path:
        return self.root / project_id / "provenance.json"

    def repair_uninitialized_repo(self, project_id: str) -> str:
        """Initialize git in a copied tree that never received .git.

        Existing files are kept. This is refused by the caller when approvals
        already name a revision, because a new history would not match them.
        """
        repo = self.repo(project_id)
        if (repo / ".git").is_dir():
            return self.head(project_id)
        if not (repo / "MEMORY.md").is_file():
            raise SsiosError(
                "memory_repo_missing",
                "canonical memory repository is incomplete; files were not deleted or replaced",
                500,
            )
        self.git.init_repo(repo)
        paths = []
        for path in sorted(repo.rglob("*")):
            if not path.is_file() or path.is_symlink() or ".git" in path.parts:
                continue
            paths.append(path.relative_to(repo).as_posix())
        if not paths:
            raise SsiosError(
                "memory_repo_missing",
                "canonical memory repository has no files to commit; nothing was deleted",
                500,
            )
        revision = self.git.commit_paths(repo, paths, "ssios seed synthetic memory")
        files = {}
        for rel in paths:
            blob = self._show(repo, rel)
            files[rel] = {
                "trust": "synthetic_seed",
                "source": "interrupted_setup",
                "payload_sha256": sha256_bytes(blob),
                "revision": revision,
                "tombstone": False,
            }
        write_json(self.provenance_path(project_id), {"project_id": project_id, "files": files})
        return revision

    def seed_project(self, project_id: str) -> str:
        self._project(project_id)
        source = self.fixtures / project_id
        repo = self.repo(project_id)
        if repo.exists():
            raise SsiosError("memory_exists", "memory repo already exists", 409)
        copy_tree_regular(source, repo)
        self.git.init_repo(repo)
        paths = self._fixture_paths(source)
        revision = self.git.commit_paths(repo, paths, "ssios seed synthetic memory")
        files = {}
        for rel in paths:
            blob = self._show(repo, rel)
            files[rel] = {
                "trust": "synthetic_seed",
                "source": "fixture",
                "payload_sha256": sha256_bytes(blob),
                "revision": revision,
                "tombstone": False,
            }
        write_json(self.provenance_path(project_id), {"project_id": project_id, "files": files})
        return revision

    def head(self, project_id: str) -> str:
        return self.git.head(self.repo(project_id))

    def core(self, project_id: str, *, budget: int, include_untrusted: bool) -> dict:
        repo = self.repo(project_id)
        revision = self.head(project_id)
        provenance = self._provenance(project_id)
        entries = []
        for rel, mode in self._ls(repo, recursive=False):
            if "/" in rel or not rel.endswith(".md"):
                continue
            if mode == "120000":
                continue
            meta = provenance["files"].get(rel, {})
            trust = str(meta.get("trust", "unknown"))
            if meta.get("tombstone"):
                continue
            if trust not in TRUSTED and not include_untrusted:
                continue
            text = self._show(repo, rel).decode("utf-8")
            front, _body = parse_frontmatter(text)
            entries.append(
                {
                    "path": rel,
                    "name": front.get("name", Path(rel).stem),
                    "description": front.get("description", ""),
                    "content": text,
                    "trust": trust,
                    "token_estimate": token_estimate(text),
                    "priority": 0 if rel == "MEMORY.md" else 1,
                }
            )
        entries.sort(key=lambda item: (item["priority"], item["path"]))
        selected = []
        used = 0
        truncated = False
        for item in entries:
            remaining = budget - used
            view = {key: item[key] for key in item if key != "priority"}
            if remaining <= 0 or item["token_estimate"] > remaining:
                truncated = True
                if remaining <= 0:
                    break
                char_budget = remaining * 4
                view["content"] = item["content"][:char_budget]
                view["token_estimate"] = token_estimate(view["content"])
                view["truncated"] = True
                selected.append(view)
                used += view["token_estimate"]
                break
            selected.append(view)
            used += item["token_estimate"]
        selected_paths = {item["path"] for item in selected}
        omitted = [item["path"] for item in entries if item["path"] not in selected_paths]
        if omitted:
            truncated = True
        return {
            "project_id": project_id,
            "revision": revision,
            "mode": "letta-memfs-compat",
            "token_estimate": used,
            "budget": budget,
            "truncated": truncated,
            "omitted": omitted,
            "files": selected,
        }

    def read_file(self, project_id: str, rel: str, *, include_untrusted: bool) -> dict:
        self._assert_no_symlink(project_id, rel)
        provenance = self._provenance(project_id)
        meta = provenance["files"].get(rel, {})
        if meta.get("tombstone"):
            return {
                "project_id": project_id,
                "path": rel,
                "revision": self.head(project_id),
                "tombstone": True,
                "trust": "tombstone",
                "content": None,
            }
        trust = str(meta.get("trust", "unknown"))
        repo = self.repo(project_id)
        try:
            blob = self._show(repo, rel)
        except SsiosError as exc:
            if exc.code == "not_found":
                raise
            raise
        if trust not in TRUSTED and not include_untrusted:
            raise SsiosError("untrusted_source", "memory file is not an approved source", 403)
        text = blob.decode("utf-8")
        return {
            "project_id": project_id,
            "path": rel,
            "revision": self.head(project_id),
            "tombstone": False,
            "trust": trust,
            "core": "/" not in rel,
            "content": text,
            "payload_sha256": sha256_bytes(blob),
        }

    def search(self, project_id: str, query: str, *, include_untrusted: bool) -> dict:
        if not query or len(query) > 200:
            raise SsiosError("search_invalid", "query rejected", 400)
        needle = query.casefold()
        provenance = self._provenance(project_id)
        hits = []
        repo = self.repo(project_id)
        for rel, mode in self._ls(repo, recursive=True):
            if mode == "120000" or not rel.endswith(".md"):
                continue
            meta = provenance["files"].get(rel, {})
            if meta.get("tombstone"):
                continue
            trust = str(meta.get("trust", "unknown"))
            if trust not in TRUSTED and not include_untrusted:
                continue
            text = self._show(repo, rel).decode("utf-8", errors="replace")
            folded = text.casefold()
            at = folded.find(needle)
            if at < 0:
                continue
            start = max(0, at - 40)
            snippet = text[start : start + 160]
            hits.append({"path": rel, "trust": trust, "snippet": snippet})
            if len(hits) >= 20:
                break
        return {
            "project_id": project_id,
            "revision": self.head(project_id),
            "search_mode": "keyword",
            "query_sha256": sha256_bytes(query.encode("utf-8")),
            "hits": hits,
        }

    def propose(self, project_id: str, expected_revision: str, files: list[dict], *, task_id: str) -> dict:
        self._check_revision(project_id, expected_revision)
        if not files:
            raise SsiosError("memory_invalid", "proposal is empty", 400)
        cleaned = []
        for item in files:
            path = str(item.get("path", ""))
            content = str(item.get("content", ""))
            validate_document(path, content)
            self._assert_parents(project_id, path)
            cleaned.append({"path": path, "content": content})
        payload = {"files": cleaned, "expected_revision": expected_revision, "project_id": project_id}
        digest = sha256_bytes(canonical_json(payload))
        proposal_id = "prop-" + secrets.token_hex(8)
        record = {
            "id": proposal_id,
            "project_id": project_id,
            "task_id": task_id,
            "kind": "commit",
            "expected_revision": expected_revision,
            "files": cleaned,
            "payload_sha256": digest,
            "trust": "untrusted",
            "status": "pending",
        }
        write_json(self.proposals / f"{proposal_id}.json", record)
        return record

    def propose_forget(self, project_id: str, expected_revision: str, path: str, *, task_id: str) -> dict:
        self._check_revision(project_id, expected_revision)
        validate_rel_parts(path)
        current = self.read_file(project_id, path, include_untrusted=True)
        if current.get("tombstone"):
            raise SsiosError("already_forgotten", "memory file is already a tombstone", 409)
        payload = {"op": "forget", "path": path, "expected_revision": expected_revision, "project_id": project_id}
        digest = sha256_bytes(canonical_json(payload))
        proposal_id = "prop-" + secrets.token_hex(8)
        record = {
            "id": proposal_id,
            "project_id": project_id,
            "task_id": task_id,
            "kind": "forget",
            "expected_revision": expected_revision,
            "path": path,
            "payload_sha256": digest,
            "trust": "untrusted",
            "status": "pending",
        }
        write_json(self.proposals / f"{proposal_id}.json", record)
        return record

    def load_proposal(self, proposal_id: str) -> dict:
        if not re.fullmatch(r"prop-[0-9a-f]{16}", proposal_id):
            raise SsiosError("not_found", "proposal not found", 404)
        path = self.proposals / f"{proposal_id}.json"
        if not path.is_file():
            raise SsiosError("not_found", "proposal not found", 404)
        return json.loads(path.read_text(encoding="utf-8"))

    def apply(self, proposal_id: str, expected_revision: str, approval_id: str, payload_sha256: str) -> str:
        proposal = self.load_proposal(proposal_id)
        if proposal["status"] != "pending":
            raise SsiosError("proposal_closed", "proposal is not pending", 409)
        if proposal["expected_revision"] != expected_revision:
            raise SsiosError("cas_conflict", "proposal revision does not match", 409)
        if self.proposal_hash(proposal) != payload_sha256:
            raise SsiosError("payload_mismatch", "proposal bytes do not match the approval", 409)
        project_id = proposal["project_id"]
        repo = self.repo(project_id)
        head = self.head(project_id)
        if head != expected_revision:
            raise SsiosError("cas_conflict", "memory revision changed", 409)
        provenance = self._provenance(project_id)
        if proposal["kind"] == "commit":
            paths = []
            for item in proposal["files"]:
                validate_document(item["path"], item["content"])
                self._assert_parents(project_id, item["path"])
                write_nofollow(repo, item["path"], item["content"].encode("utf-8"))
                paths.append(item["path"])
            message = f"ssios approved memory {proposal_id}"
            revision = self.git.commit_paths(repo, paths, message)
            for item in proposal["files"]:
                blob = item["content"].encode("utf-8")
                provenance["files"][item["path"]] = {
                    "trust": "approved",
                    "source": "agent_proposal",
                    "approval_id": approval_id,
                    "payload_sha256": sha256_bytes(blob),
                    "revision": revision,
                    "tombstone": False,
                }
        elif proposal["kind"] == "forget":
            rel = proposal["path"]
            self._assert_no_symlink(project_id, rel)
            message = f"ssios forget {proposal_id}"
            revision = self.git.remove_path(repo, rel, message)
            previous = provenance["files"].get(rel, {})
            provenance["files"][rel] = {
                "trust": "tombstone",
                "source": "forget",
                "approval_id": approval_id,
                "payload_sha256": previous.get("payload_sha256"),
                "revision": revision,
                "tombstone": True,
            }
        else:
            raise SsiosError("memory_invalid", "unknown proposal kind", 400)
        proposal["status"] = "committed"
        proposal["committed_revision"] = revision
        write_json(self.provenance_path(project_id), provenance)
        write_json(self.proposals / f"{proposal_id}.json", proposal)
        return revision

    @staticmethod
    def proposal_hash(proposal: dict) -> str:
        if proposal["kind"] == "commit":
            payload = {
                "expected_revision": proposal["expected_revision"],
                "files": proposal["files"],
                "project_id": proposal["project_id"],
            }
        elif proposal["kind"] == "forget":
            payload = {
                "expected_revision": proposal["expected_revision"],
                "op": "forget",
                "path": proposal["path"],
                "project_id": proposal["project_id"],
            }
        else:
            raise SsiosError("memory_invalid", "unknown proposal kind", 400)
        return sha256_bytes(canonical_json(payload))

    def mark_proposal(self, proposal_id: str, status: str) -> None:
        proposal = self.load_proposal(proposal_id)
        proposal["status"] = status
        write_json(self.proposals / f"{proposal_id}.json", proposal)

    def _check_revision(self, project_id: str, expected_revision: str) -> None:
        if not _SHA.fullmatch(expected_revision or ""):
            raise SsiosError("revision_invalid", "revision rejected", 400)
        if self.head(project_id) != expected_revision:
            raise SsiosError("cas_conflict", "memory revision changed", 409)

    def _provenance(self, project_id: str) -> dict:
        data = read_json(self.provenance_path(project_id), None)
        if not isinstance(data, dict):
            raise SsiosError("provenance_missing", "memory provenance missing", 500)
        data.setdefault("files", {})
        return data

    def _show(self, repo: Path, rel: str) -> bytes:
        validate_rel_parts(rel)
        completed = self.git.run(repo, ["show", f"HEAD:{rel}"], check=False)
        if completed.returncode != 0:
            raise SsiosError("not_found", "not found", 404)
        return completed.stdout

    def _ls(self, repo: Path, *, recursive: bool) -> list[tuple[str, str]]:
        args = ["ls-tree", "-z"]
        if recursive:
            args.append("-r")
        args.append("HEAD")
        completed = self.git.run(repo, args)
        raw = completed.stdout
        items = []
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            meta, name = entry.split(b"\t", 1)
            mode = meta.split(b" ", 1)[0].decode()
            rel = name.decode("utf-8")
            items.append((rel, mode))
        return items

    def _assert_no_symlink(self, project_id: str, rel: str) -> None:
        parts = validate_rel_parts(rel)
        cur = self.repo(project_id)
        for index, part in enumerate(parts):
            cur = cur / part
            if cur.is_symlink():
                raise SsiosError("symlink_rejected", "symlink rejected", 400)
            final = index == len(parts) - 1
            if not final and not cur.is_dir():
                raise SsiosError("not_found", "not found", 404)

    def _assert_parents(self, project_id: str, rel: str) -> None:
        parts = validate_rel_parts(rel)
        cur = self.repo(project_id)
        for part in parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                raise SsiosError("symlink_rejected", "symlink rejected", 400)
            if cur.exists() and not cur.is_dir():
                raise SsiosError("path_rejected", "path rejected", 400)

    def _project(self, project_id: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", project_id):
            raise SsiosError("project_invalid", "project id rejected", 400)

    @staticmethod
    def _fixture_paths(source: Path) -> list[str]:
        paths = []
        for path in sorted(source.rglob("*")):
            if path.is_file() and not path.is_symlink():
                paths.append(path.relative_to(source).as_posix())
        return paths


def worktree_has_phrase(repo: Path, phrase: str) -> bool:
    for path in repo.rglob("*"):
        if ".git" in path.parts or not path.is_file() or path.is_symlink():
            continue
        if phrase.encode() in path.read_bytes():
            return True
    return False

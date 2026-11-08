"""Project exports, per-task staging, and CAS publish."""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from ssios.errors import SsiosError
from ssios.util import (
    copy_tree_regular,
    read_json,
    read_nofollow,
    secure_mkdir,
    sha256_bytes,
    walk_nofollow,
    write_json,
    write_nofollow,
)

_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_REV = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")


class ProjectStorage:
    def __init__(self, state_dir: Path, fixtures: Path) -> None:
        self.root = state_dir / "storage"
        self.fixtures = fixtures / "exports"
        self.exports = self.root / "exports"
        self.tasks = self.root / "tasks"
        self.published = self.root / "published"

    def seed_export(self, project_id: str, revision: str) -> None:
        self._ids(project_id, revision)
        source = self.fixtures / project_id / revision
        dest = self.exports / project_id / revision
        if dest.exists():
            raise SsiosError("export_exists", "export already exists", 409)
        copy_tree_regular(source, dest)
        files = {}
        for path in sorted(dest.rglob("*")):
            if path.is_file() and not path.is_symlink():
                rel = path.relative_to(dest).as_posix()
                files[rel] = sha256_bytes(path.read_bytes())
                path.chmod(0o444)
        write_json(dest / "manifest.json", {"revision": revision, "files": files}, mode=0o444)
        head = self.published / project_id
        head.mkdir(parents=True, exist_ok=True)
        write_json(head / "HEAD.json", {"revision": "none"})

    def manifest(self, project_id: str, revision: str) -> dict:
        self._ids(project_id, revision)
        path = self.exports / project_id / revision / "manifest.json"
        data = read_json(path, None)
        if not isinstance(data, dict):
            raise SsiosError("not_found", "export not found", 404)
        return data

    def read_export(self, project_id: str, revision: str, rel: str) -> dict:
        manifest = self.manifest(project_id, revision)
        files = manifest.get("files", {})
        if rel not in files:
            raise SsiosError("not_found", "export path not found", 404)
        data = read_nofollow(self.exports / project_id / revision, rel)
        digest = sha256_bytes(data)
        if digest != files[rel]:
            raise SsiosError("export_integrity", "export bytes do not match the manifest", 409)
        return {
            "project_id": project_id,
            "revision": revision,
            "path": rel,
            "sha256": digest,
            "content": data.decode("utf-8"),
        }

    def stage(self, task_id: str, filename: str, data: bytes) -> dict:
        self._task(task_id)
        if filename != "summary.md":
            raise SsiosError("path_rejected", "only summary.md can be staged", 400)
        root = self.tasks / task_id / "staged"
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        write_nofollow(root, filename, data)
        digest = sha256_bytes(data)
        write_json(self.tasks / task_id / "staged.json", {"path": filename, "sha256": digest, "frozen": False})
        return {"path": filename, "sha256": digest}

    def freeze(self, task_id: str) -> None:
        meta_path = self.tasks / task_id / "staged.json"
        meta = read_json(meta_path, None)
        if not isinstance(meta, dict):
            raise SsiosError("not_found", "staged output missing", 404)
        meta["frozen"] = True
        write_json(meta_path, meta)

    def is_frozen(self, task_id: str) -> bool:
        meta = read_json(self.tasks / task_id / "staged.json", {})
        return bool(isinstance(meta, dict) and meta.get("frozen"))

    def staged_bytes(self, task_id: str) -> bytes:
        return read_nofollow(self.tasks / task_id / "staged", "summary.md")

    def published_head(self, project_id: str) -> str:
        self._project(project_id)
        data = read_json(self.published / project_id / "HEAD.json", {"revision": "none"})
        if not isinstance(data, dict):
            return "none"
        return str(data.get("revision", "none"))

    def publish(self, project_id: str, task_id: str, expected_revision: str, payload_sha256: str) -> str:
        head = self.published_head(project_id)
        if head != expected_revision:
            raise SsiosError("cas_conflict", "published revision changed", 409)
        data = self.staged_bytes(task_id)
        digest = sha256_bytes(data)
        if digest != payload_sha256:
            raise SsiosError("payload_mismatch", "staged bytes do not match the approval", 409)
        new_rev = "pub-" + secrets.token_hex(4)
        dest = self.published / project_id / "revisions" / new_rev
        secure_mkdir(dest)
        write_nofollow(dest, "summary.md", data, mode=0o444)
        write_json(
            dest / "manifest.json",
            {"revision": new_rev, "files": {"summary.md": digest}},
            mode=0o444,
        )
        write_json(self.published / project_id / "HEAD.json", {"revision": new_rev})
        return new_rev

    def read_published(self, project_id: str, revision: str, rel: str = "summary.md") -> dict:
        if revision == "HEAD":
            revision = self.published_head(project_id)
        if not _REV.fullmatch(revision) or revision == "none":
            raise SsiosError("not_found", "published revision not found", 404)
        if rel != "summary.md":
            raise SsiosError("path_rejected", "path rejected", 400)
        root = self.published / project_id / "revisions" / revision
        manifest = read_json(root / "manifest.json", None)
        if not isinstance(manifest, dict):
            raise SsiosError("not_found", "published revision not found", 404)
        data = read_nofollow(root, rel)
        digest = sha256_bytes(data)
        if digest != manifest["files"][rel]:
            raise SsiosError("export_integrity", "published bytes do not match the manifest", 409)
        return {"project_id": project_id, "revision": revision, "path": rel, "sha256": digest, "content": data.decode("utf-8")}

    def task_dir(self, task_id: str) -> Path:
        self._task(task_id)
        path = self.tasks / task_id
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    def _ids(self, project_id: str, revision: str) -> None:
        self._project(project_id)
        if not _REV.fullmatch(revision):
            raise SsiosError("revision_invalid", "revision rejected", 400)

    def _project(self, project_id: str) -> None:
        if not _PROJECT.fullmatch(project_id):
            raise SsiosError("project_invalid", "project id rejected", 400)

    def _task(self, task_id: str) -> None:
        if not re.fullmatch(r"task-[0-9a-f]{16}", task_id):
            raise SsiosError("task_invalid", "task id rejected", 400)


def export_root(storage: ProjectStorage, project_id: str, revision: str) -> Path:
    walk_nofollow(storage.exports / project_id, revision, expect="dir")
    return storage.exports / project_id / revision

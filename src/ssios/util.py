"""Small filesystem and hashing helpers. No shell."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from pathlib import Path

from ssios.errors import SsiosError


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def token_estimate(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def chmod_tree_private(path: Path) -> None:
    if path.is_dir():
        os.chmod(path, 0o700)
        for child in path.iterdir():
            chmod_tree_private(child)
    elif path.exists():
        os.chmod(path, 0o600)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write beside the file, then rename over it.

    Each writer has its own temporary name. With one shared name, two requests
    saving the same file at once wrote into each other's bytes, and the slower
    one failed because its temporary file had already been renamed away.
    """
    secure_mkdir(path.parent)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            os.write(fd, data)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: object, mode: int = 0o600) -> None:
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True).encode("utf-8") + b"\n", mode)


def assert_private(path: Path) -> None:
    if not path.exists():
        raise SsiosError("insecure_permissions", f"missing {path.name}", 500)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SsiosError("insecure_permissions", f"{path.name} is group or world accessible", 500)


def validate_rel_parts(rel: str) -> list[str]:
    if not isinstance(rel, str) or not rel:
        raise SsiosError("path_rejected", "path rejected", 400)
    if "\\" in rel or "\x00" in rel or ":" in rel or rel.startswith("/") or rel.startswith("~"):
        raise SsiosError("path_rejected", "path rejected", 400)
    parts = rel.split("/")
    if any(part in ("", ".", "..", ".git", ".ssios") or part.startswith("-") or part.startswith(".") for part in parts):
        raise SsiosError("path_rejected", "path rejected", 400)
    return parts


def reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise SsiosError("symlink_rejected", "symlink rejected", 400)


def walk_nofollow(root: Path, rel: str, *, expect: str) -> Path:
    parts = validate_rel_parts(rel)
    root_real = root.resolve()
    cur = root_real
    for index, part in enumerate(parts):
        nxt = cur / part
        final = index == len(parts) - 1
        reject_symlink(nxt)
        if not final:
            if not nxt.is_dir():
                raise SsiosError("not_found", "not found", 404)
        elif expect == "file":
            if not nxt.exists():
                raise SsiosError("not_found", "not found", 404)
            if not nxt.is_file():
                raise SsiosError("path_rejected", "path rejected", 400)
        elif expect == "dir":
            if not nxt.is_dir():
                raise SsiosError("not_found", "not found", 404)
        real = nxt.resolve()
        if root_real != real and root_real not in real.parents:
            raise SsiosError("path_rejected", "path rejected", 400)
        cur = nxt
    return cur


def ensure_parent_chain(root: Path, rel: str) -> Path:
    parts = validate_rel_parts(rel)
    root_real = root.resolve()
    cur = root_real
    for part in parts[:-1]:
        nxt = cur / part
        reject_symlink(nxt)
        if not nxt.exists():
            os.mkdir(nxt, 0o700)
        elif not nxt.is_dir():
            raise SsiosError("path_rejected", "path rejected", 400)
        real = nxt.resolve()
        if root_real != real and root_real not in real.parents:
            raise SsiosError("path_rejected", "path rejected", 400)
        cur = nxt
    final = cur / parts[-1]
    reject_symlink(final)
    return final


def write_nofollow(root: Path, rel: str, data: bytes, mode: int = 0o600) -> Path:
    final = ensure_parent_chain(root, rel)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(final, flags, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(final, mode)
    return final


def read_nofollow(root: Path, rel: str, limit: int = 2_000_000) -> bytes:
    path = walk_nofollow(root, rel, expect="file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SsiosError("path_rejected", "path rejected", 400)
        if st.st_size > limit:
            raise SsiosError("too_large", "file too large", 400)
        return os.read(fd, st.st_size)
    finally:
        os.close(fd)


def copy_tree_regular(src: Path, dst: Path) -> None:
    if not src.is_dir():
        raise SsiosError("seed_missing", "fixture directory missing", 500)
    secure_mkdir(dst)
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        current = Path(dirpath)
        rel = current.relative_to(src)
        target = dst if rel == Path(".") else dst / rel
        secure_mkdir(target)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            source = current / name
            if source.is_symlink() or not source.is_file():
                raise SsiosError("seed_symlink", "fixture contains a symlink", 500)
            data = source.read_bytes()
            write_nofollow(dst, str((rel / name).as_posix()) if rel != Path(".") else name, data)

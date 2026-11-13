"""Back up and restore the state directory.

An archive is a gzip tar with a manifest of every file's size, mode, and
SHA-256. It is not encrypted. Keep it on an encrypted disk. Google tokens are
in the OS keychain, not in the state directory, so they are not in the archive.
A restore writes only into a directory that does not exist or is empty.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import time
from pathlib import Path, PurePosixPath

from ssios import __version__
from ssios.errors import SsiosError

MANIFEST = "manifest.json"
FORMAT = 1
# Rebuilt on the next start or by vm-prepare. Not household data.
SKIP_TOP = ("run", "cache", "listen.url")
_CHUNK = 1 << 20


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def ui_running(state_dir: Path) -> bool:
    """True when run/ui.pid names a live process."""
    try:
        pid = int((Path(state_dir) / "run" / "ui.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _walk(state_dir: Path) -> tuple[list[tuple[str, Path]], list[str], list[str]]:
    files: list[tuple[str, Path]] = []
    dirs: list[str] = []
    skipped: list[str] = []
    for root, names, leaves in os.walk(state_dir, followlinks=False):
        base = Path(root)
        rel_root = base.relative_to(state_dir)
        if rel_root == Path("."):
            names[:] = [name for name in names if name not in SKIP_TOP]
            leaves = [name for name in leaves if name not in SKIP_TOP]
        for name in sorted(list(names)):
            path = base / name
            if path.is_symlink():
                names.remove(name)
                skipped.append((rel_root / name).as_posix())
            else:
                dirs.append((rel_root / name).as_posix())
        names.sort()
        for name in sorted(leaves):
            path = base / name
            rel = (rel_root / name).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode):
                files.append((rel, path))
            else:
                skipped.append(rel)
    return files, dirs, skipped


def create_backup(state_dir: Path, out_dir: Path, *, allow_running: bool = False, now: float | None = None) -> dict:
    state = Path(state_dir).resolve()
    if not state.is_dir():
        raise SsiosError("backup_rejected", "The state directory does not exist.", 400)
    target = Path(out_dir).expanduser().resolve()
    if target == state or state in target.parents:
        raise SsiosError("backup_rejected", "The backup directory must be outside the state directory.", 400)
    if ui_running(state) and not allow_running:
        raise SsiosError("backup_running", "SSIOS is running. Stop it first, or pass --allow-running and accept that a file may be caught mid-write.", 409)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    archive = target / f"ssios-backup-{stamp}.tar.gz"
    if archive.exists():
        raise SsiosError("backup_rejected", "A backup with this timestamp already exists.", 409)
    files, dirs, skipped = _walk(state)
    entries = []
    total = 0
    partial = target / (archive.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for rel, path in files:
                try:
                    info = tar.gettarinfo(str(path), arcname="state/" + rel)
                    digest = _sha(path)
                    with path.open("rb") as handle:
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        tar.addfile(info, handle)
                except OSError:
                    skipped.append(rel)
                    continue
                entries.append({"path": rel, "size": int(info.size), "mode": stat.S_IMODE(info.mode), "sha256": digest})
                total += int(info.size)
            manifest = {
                "format": FORMAT,
                "ssios_version": __version__,
                "created_at": stamp,
                "dirs": dirs,
                "files": entries,
                "skipped": sorted(skipped),
                "not_included": list(SKIP_TOP) + ["OS keychain items (Google tokens and the OAuth client secret)", "libraries stored in their own folder outside the state directory"],
            }
            body = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(body)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(body))
        os.replace(partial, archive)
    finally:
        if partial.exists():
            partial.unlink()
    return {"archive": str(archive), "files": len(entries), "bytes": total, "skipped": sorted(skipped), "sha256": _sha(archive)}


def _safe_rel(name: str) -> str:
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts) or "\\" in name or "\x00" in name:
        raise SsiosError("backup_invalid", "The archive names a path outside the state directory.", 400)
    return pure.as_posix()


def _read_manifest(tar: tarfile.TarFile) -> dict:
    try:
        member = tar.getmember(MANIFEST)
        handle = tar.extractfile(member)
        manifest = json.loads(handle.read(64 << 20).decode("utf-8")) if handle else None
    except (KeyError, UnicodeError, json.JSONDecodeError, tarfile.TarError) as exc:
        raise SsiosError("backup_invalid", "The archive has no readable manifest.", 400) from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), list):
        raise SsiosError("backup_invalid", "The manifest format is not supported.", 400)
    return manifest


def _check(archive: Path, dest: Path | None) -> dict:
    """Compare every member with the manifest. With dest, also write the files there."""
    try:
        tar = tarfile.open(archive, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise SsiosError("backup_invalid", "The archive cannot be opened.", 400) from exc
    with tar:
        manifest = _read_manifest(tar)
        wanted = {}
        for row in manifest["files"]:
            if not isinstance(row, dict):
                raise SsiosError("backup_invalid", "The manifest has an unreadable row.", 400)
            wanted[_safe_rel(str(row.get("path") or ""))] = row
        if dest is not None:
            for rel in manifest.get("dirs") or []:
                folder = dest / _safe_rel(str(rel))
                folder.mkdir(parents=True, exist_ok=True)
                os.chmod(folder, 0o700)
        seen = set()
        total = 0
        for member in tar:
            if member.name == MANIFEST:
                continue
            if not member.name.startswith("state/") or not member.isreg():
                raise SsiosError("backup_invalid", "The archive holds something that is not a plain state file.", 400)
            rel = _safe_rel(member.name[len("state/") :])
            row = wanted.get(rel)
            if row is None or rel in seen or int(row.get("size", -1)) != member.size:
                raise SsiosError("backup_invalid", "The archive does not match its manifest.", 400)
            seen.add(rel)
            source = tar.extractfile(member)
            digest = hashlib.sha256()
            out = None
            path = None
            if dest is not None:
                path = dest / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                os.chmod(path.parent, 0o700)
                out = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
            try:
                for block in iter(lambda: source.read(_CHUNK), b""):
                    digest.update(block)
                    if out is not None:
                        out.write(block)
            finally:
                if out is not None:
                    out.close()
            if digest.hexdigest() != str(row.get("sha256") or ""):
                raise SsiosError("backup_invalid", "A file in the archive does not match its recorded hash.", 400)
            if path is not None:
                os.chmod(path, int(row.get("mode") or 0o600) & 0o700)
            total += member.size
        if seen != set(wanted):
            raise SsiosError("backup_invalid", "The archive is missing files the manifest lists.", 400)
    return {"files": len(seen), "bytes": total, "ssios_version": str(manifest.get("ssios_version") or ""), "created_at": str(manifest.get("created_at") or "")}


def verify_backup(archive: Path) -> dict:
    report = _check(Path(archive), None)
    report["ok"] = True
    return report


def restore_backup(archive: Path, state_dir: Path) -> dict:
    """Verify first, then write. The destination must be missing or empty, so nothing is overwritten."""
    dest = Path(state_dir).expanduser().resolve()
    if dest.is_symlink() or (dest.exists() and (not dest.is_dir() or any(dest.iterdir()))):
        raise SsiosError("restore_rejected", "The destination must be a directory that does not exist or is empty. Nothing was written.", 409)
    _check(Path(archive), None)
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    report = _check(Path(archive), dest)
    report["state"] = str(dest)
    report["next"] = "Start SSIOS with this state directory, run audit-verify, then reconnect Google on the Connections page."
    return report

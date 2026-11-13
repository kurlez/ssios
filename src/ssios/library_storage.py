"""Where a library keeps its files. The default is the state directory. A library can also live in a
folder the owner names, including one on an external disk.

A library outside the state directory has a marker file. When the marker is missing, the disk is not
connected, and nothing is created in its place. Such a library is not in `ssios backup`.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from pathlib import Path

from ssios.errors import SsiosError
from ssios.locale_messages import say
from ssios.util import read_json, write_json

_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def storage_parent(plane, account_id: str, location: str) -> Path:
    """An existing directory on this computer or a connected disk. Network addresses, links, and the state directory are refused."""
    from ssios.library import _lang

    lang = _lang(plane, account_id)
    text = str(location or "").strip()
    bad = SsiosError("library_location", say(lang, "library_location"), 400)
    if not text or text.startswith("//") or text.startswith("\\\\") or _SCHEME.match(text) or "\x00" in text:
        raise bad
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise bad
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise bad
    if not current.is_dir():
        raise bad
    resolved = current.resolve()
    state = Path(plane.settings.state_dir).resolve()
    if resolved == state or state in resolved.parents or resolved in state.parents:
        raise bad
    return resolved


def _probe_permissions(folder: Path) -> str:
    """Whether this disk keeps owner-only permissions. exFAT and FAT do not."""
    probe = folder / ".ssios-probe"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.chmod(folder, 0o700)
        kept = stat.S_IMODE(probe.stat().st_mode) & 0o077 == 0 and stat.S_IMODE(folder.stat().st_mode) & 0o077 == 0
    except OSError as exc:
        raise SsiosError("library_location", "This disk refused a permission change, so a library cannot be kept on it. APFS and Mac OS Extended work.", 400) from exc
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return "enforced" if kept else "not enforced"


def new_root(plane, account_id: str, library_id: str, name: str, location: str) -> dict:
    """Create the library's own folder inside the chosen directory and write its marker."""
    from ssios.library import MARKER

    parent = storage_parent(plane, account_id, location)
    safe = re.sub(r"[^\w\- ]+", " ", name, flags=re.UNICODE).strip()[:40] or "Files"
    root = parent / f"{safe} ({library_id}).ssioslibrary"
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise SsiosError("library_location", "A folder with that library's name is already there.", 409) from exc
    except OSError as exc:
        raise SsiosError("library_location", "That folder could not be written.", 400) from exc
    permissions = _probe_permissions(root)
    write_json(root / MARKER, {"id": library_id, "owner_id": account_id, "name": name, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return {"root": root, "permissions": permissions}


def _tree(root: Path, skip: set[str]) -> dict[str, int]:
    found = {}
    for base, names, leaves in os.walk(root, followlinks=False):
        rel = Path(base).relative_to(root)
        if rel == Path("."):
            names[:] = [name for name in names if name not in skip and not name.startswith(".moved-")]
            leaves = [name for name in leaves if name not in skip]
        for name in leaves:
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            found[(rel / name).as_posix()] = path.stat().st_size
    return found


def move_library(plane, account_id: str, library_id: str, location: str, peer_ip: str) -> dict:
    """Copy a library to a new folder, check the copy, and switch to it. The old files are set aside, not deleted."""
    from ssios.library import MARKER, _audit, _library_root, _owned, _require, _shared_acl_path

    # The library must be in this account's own list. An id that is only well formed is not enough.
    _owned(plane, account_id, library_id)
    _require(plane, account_id, library_id, "owner")
    old = Path(_library_root(plane, account_id, library_id))
    name = str(read_json(old / "metadata.json", {}).get("name") or "Files")
    made = new_root(plane, account_id, library_id, name, location)
    new = Path(made["root"])
    skip = {"acl.json", MARKER}
    try:
        before = _tree(old, skip)
        for rel in before:
            target = new / rel
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(old / rel, target)
        after = _tree(new, skip)
        if before != after:
            raise SsiosError("library_location", "The copy did not match the original. Nothing was switched.", 500)
    except (OSError, SsiosError) as exc:
        shutil.rmtree(new, ignore_errors=True)
        if isinstance(exc, SsiosError):
            raise
        raise SsiosError("library_location", "The copy failed, for example because the disk is full. Nothing was switched.", 500) from exc
    acl_path = _shared_acl_path(plane, library_id)
    if acl_path.is_file():
        acl = read_json(acl_path, {})
        acl["root"] = str(new)
        write_json(acl_path, acl)
    else:
        index_path = plane.accounts.home(account_id) / "library" / "libraries.json"
        index = read_json(index_path, {"libraries": []})
        for row in index.get("libraries") or []:
            if isinstance(row, dict) and row.get("id") == library_id:
                row["root"] = str(new)
                row["permissions"] = made["permissions"]
        write_json(index_path, index)
    aside = old / (".moved-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    aside.mkdir(mode=0o700)
    for entry in list(old.iterdir()):
        if entry == aside or entry.name == "acl.json" or entry.name.startswith(".moved-"):
            continue
        entry.rename(aside / entry.name)
    _audit(plane, account_id, peer_ip, "library.move", library_id, "ok", sum(before.values()))
    return {"library_id": library_id, "root": str(new), "permissions": made["permissions"], "files": len(before), "bytes": sum(before.values()), "old_copy": str(aside)}

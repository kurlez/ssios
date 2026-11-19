"""The catalog of a Files library: every item row, read once and kept.

Each item stays one JSON file under ``items/``, which remains the truth. This module keeps a copy of the rows in
memory and in ``catalog-cache.json`` next to them, keyed by each file's modification time, size, and inode. A
listing reads the folder once and parses only the files whose key changed, so a page of a 10,000-item library
does not parse 10,000 files, a restart does not either, and a file changed on disk by a sync tool or by hand is
read again. The cache file can be deleted at any time; it is rebuilt from the item files.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ssios.util import atomic_write

CACHE = "catalog-cache.json"
_VERSION = 1
_LOCK = threading.Lock()
# items folder -> {file name: (key, row)}
_MEMORY: dict[str, dict[str, tuple[tuple[int, int, int], dict]]] = {}
_STATS = {"parsed": 0, "disk_loads": 0, "disk_writes": 0}


def _key(entry: os.DirEntry) -> tuple[int, int, int] | None:
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    return (int(info.st_mtime_ns), int(info.st_size), int(info.st_ino))


def _load_disk(root: Path) -> dict[str, tuple[tuple[int, int, int], dict]]:
    path = root / CACHE
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _VERSION or not isinstance(data.get("rows"), dict):
        return {}
    _STATS["disk_loads"] += 1
    known: dict[str, tuple[tuple[int, int, int], dict]] = {}
    for name, value in data["rows"].items():
        if isinstance(value, list) and len(value) == 4 and isinstance(value[3], dict):
            try:
                known[str(name)] = ((int(value[0]), int(value[1]), int(value[2])), value[3])
            except (TypeError, ValueError):
                continue
    return known


def _save_disk(root: Path, known: dict[str, tuple[tuple[int, int, int], dict]]) -> None:
    rows = {name: [key[0], key[1], key[2], row] for name, (key, row) in known.items()}
    try:
        atomic_write(root / CACHE, json.dumps({"version": _VERSION, "rows": rows}, separators=(",", ":")).encode("utf-8"))
        _STATS["disk_writes"] += 1
    except OSError:
        # A read-only or full disk only costs speed: the item files are still read.
        pass


def rows(root: Path) -> list[dict]:
    """Every item row in the library at ``root``, in file-name order. The caller gets its own copies."""
    folder = root / "items"
    if not folder.is_dir():
        return []
    slot = str(folder)
    with _LOCK:
        known = _MEMORY.get(slot)
        loaded_from_disk = False
        if known is None:
            known = _load_disk(root)
            loaded_from_disk = True
        fresh: dict[str, tuple[tuple[int, int, int], dict]] = {}
        changed = loaded_from_disk and not known
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return []
        for entry in entries:
            name = entry.name
            if not (name.startswith("item-") and name.endswith(".json")):
                continue
            if entry.is_symlink():
                continue
            key = _key(entry)
            if key is None:
                continue
            seen = known.get(name)
            if seen is not None and seen[0] == key:
                fresh[name] = seen
                continue
            try:
                data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                changed = True
                continue
            _STATS["parsed"] += 1
            changed = True
            if isinstance(data, dict) and data.get("id"):
                fresh[name] = (key, data)
        if len(fresh) != len(known):
            changed = True
        _MEMORY[slot] = fresh
        if changed:
            _save_disk(root, fresh)
        ordered = [fresh[name][1] for name in sorted(fresh)]
    return [_copy(row) for row in ordered]


_NESTED = ("tags", "folder_ids", "versions", "palette", "suggested_tags", "auto_tags")


def _copy(row: dict) -> dict:
    """A copy deep enough that a caller changing tags, folders, or versions does not change the cache."""
    out = dict(row)
    for name in _NESTED:
        value = out.get(name)
        if isinstance(value, list):
            out[name] = [dict(one) if isinstance(one, dict) else one for one in value]
    return out


def forget(root: Path | None = None) -> None:
    """Drop the memory copy, as a restart would. The cache file stays."""
    with _LOCK:
        if root is None:
            _MEMORY.clear()
        else:
            _MEMORY.pop(str(Path(root) / "items"), None)


def stats() -> dict:
    return dict(_STATS)

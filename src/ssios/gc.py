"""Remove disposable Letta export copies. Git history under memfs is never touched."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ssios.errors import SsiosError

_AGENT = re.compile(r"^agent-local-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SNAPSHOT = re.compile(r"^[0-9a-f]{4}$")


def leftover_exports(state_dir: Path) -> list[Path]:
    root = (state_dir / "upstream-letta" / "exports").resolve()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for agent in sorted(root.iterdir()):
        if agent.is_symlink() or not agent.is_dir() or not _AGENT.fullmatch(agent.name):
            continue
        for child in sorted(agent.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            if child.name == "scratch" or _SNAPSHOT.fullmatch(child.name):
                found.append(child.resolve())
    return found


def collect(state_dir: Path, *, apply: bool) -> dict:
    paths = leftover_exports(state_dir)
    relative = []
    for path in paths:
        try:
            relative.append(path.relative_to(state_dir.resolve()).as_posix())
        except ValueError as exc:
            raise SsiosError("gc_rejected", "cleanup path escaped the state directory", 500) from exc
    if not apply:
        return {"dry_run": True, "removed": 0, "count": len(relative), "paths": relative}
    memory_root = (state_dir / "upstream-letta" / "backend" / "memfs").resolve()
    removed = 0
    for path in paths:
        if memory_root == path or memory_root in path.parents:
            raise SsiosError("gc_rejected", "cleanup refused a memory repository", 500)
        shutil.rmtree(path)
        removed += 1
    return {"dry_run": False, "removed": removed, "count": removed, "paths": relative}

"""Keep an account's Notes vault and an Obsidian folder the same, in both directions.

The simplest setup needs no sync: `ssios vault-set` makes Notes use the Obsidian folder itself. This tool is
for keeping two separate folders, for example when the Obsidian vault is in iCloud Drive or on another disk.

Rules:
- Only notes and attachments in ordinary folders are synced. `.obsidian`, `.trash`, other hidden folders,
  hidden files, and symbolic links are left alone on both sides.
- Nothing is overwritten without knowing which side changed. The hashes from the last sync are kept in the
  state directory. A file changed on one side is copied to the other. A file changed on both sides is kept
  on both sides, and the other side's version is added beside it with "conflict" in its name.
- Nothing is deleted. A file removed on one side, and unchanged on the other, is moved into that vault's
  `.trash` folder, which is where Obsidian puts deleted notes. An edit on one side wins over a delete on the other.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

from ssios.errors import SsiosError
from ssios.util import read_json, write_json

SUFFIXES = (".md", ".canvas", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".mp3", ".m4a", ".wav", ".mp4", ".mov", ".webm")
MAX_BYTES = 100_000_000
MAX_FILES = 20_000


def _files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for base, names, leaves in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if not name.startswith(".") and not (Path(base) / name).is_symlink())
        for leaf in sorted(leaves):
            path = Path(base) / leaf
            if leaf.startswith(".") or not leaf.casefold().endswith(SUFFIXES) or path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > MAX_BYTES:
                continue
            found[path.relative_to(root).as_posix()] = path
            if len(found) >= MAX_FILES:
                return found
    return found


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.ssios-sync-{os.getpid()}")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _to_trash(root: Path, rel: str, stamp: str) -> None:
    target = root / ".trash" / rel
    if target.exists():
        target = target.with_name(f"{target.stem} ({stamp}){target.suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(root / rel, target)


def _conflict_name(rel: str, side: str, stamp: str) -> str:
    path = Path(rel)
    return (path.parent / f"{path.stem} (conflict from {side} {stamp}){path.suffix}").as_posix()


def peer_folder(settings, location: str) -> Path:
    """The Obsidian folder. It must exist, be on this computer or a connected disk, and be outside the state directory."""
    text = str(location or "").strip()
    bad = SsiosError("sync_folder", "That folder cannot be synced. Use an existing folder on this computer or a connected disk, outside the SSIOS state directory.", 400)
    if not text or text.startswith("//") or "://" in text or "\x00" in text:
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
    state = Path(settings.state_dir).resolve()
    if resolved == state or state in resolved.parents or resolved in state.parents:
        raise bad
    return resolved


def sync_vault(settings, account_id: str, location: str, *, dry_run: bool = False, now: float | None = None) -> dict:
    from ssios.vault import account_vault

    mine = account_vault(settings, account_id)
    theirs = peer_folder(settings, location)
    if mine.resolve() == theirs or theirs in mine.resolve().parents or mine.resolve() in theirs.parents:
        raise SsiosError("sync_folder", "Notes already uses that folder, so there is nothing to sync.", 400)
    mine.mkdir(parents=True, exist_ok=True)
    state_path = Path(settings.state_dir) / "accounts" / account_id / "vault-sync.json"
    saved = read_json(state_path, {})
    if not isinstance(saved, dict):
        saved = {}
    base = saved.get(str(theirs)) if isinstance(saved.get(str(theirs)), dict) else {}
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    left, right = _files(mine), _files(theirs)
    report: dict = {"notes": str(mine), "obsidian": str(theirs), "dry_run": bool(dry_run), "to_obsidian": [], "to_notes": [], "conflicts": [], "trashed_in_notes": [], "trashed_in_obsidian": [], "same": 0}
    after: dict[str, str] = {}
    for rel in sorted(set(left) | set(right) | set(base)):
        a = _sha(left[rel]) if rel in left else ""
        b = _sha(right[rel]) if rel in right else ""
        was = str(base.get(rel) or "")
        if a and b:
            if a == b:
                report["same"] += 1
                after[rel] = a
            elif was and a == was:
                report["to_notes"].append(rel)
                if not dry_run:
                    _copy(right[rel], mine / rel)
                after[rel] = b
            elif was and b == was:
                report["to_obsidian"].append(rel)
                if not dry_run:
                    _copy(left[rel], theirs / rel)
                after[rel] = a
            else:
                # Changed on both sides, or never synced and different. Both versions are kept on both sides.
                report["conflicts"].append(rel)
                if not dry_run:
                    _copy(right[rel], mine / _conflict_name(rel, "Obsidian", stamp))
                    _copy(left[rel], theirs / _conflict_name(rel, "SSIOS", stamp))
        elif a:
            if was and a == was:
                # Removed in Obsidian and not touched here.
                report["trashed_in_notes"].append(rel)
                if not dry_run:
                    _to_trash(mine, rel, stamp)
            else:
                report["to_obsidian"].append(rel)
                if not dry_run:
                    _copy(left[rel], theirs / rel)
                after[rel] = a
        elif b:
            if was and b == was:
                report["trashed_in_obsidian"].append(rel)
                if not dry_run:
                    _to_trash(theirs, rel, stamp)
            else:
                report["to_notes"].append(rel)
                if not dry_run:
                    _copy(right[rel], mine / rel)
                after[rel] = b
    if not dry_run:
        saved[str(theirs)] = after
        write_json(state_path, saved)
    counts = {key: len(report[key]) for key in ("to_obsidian", "to_notes", "conflicts", "trashed_in_notes", "trashed_in_obsidian")}
    for key in counts:
        report[key] = report[key][:200]
    report["counts"] = counts
    return report

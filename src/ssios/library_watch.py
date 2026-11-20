"""An opt-in watch folder. The path is on this computer, and the switch starts off."""

from __future__ import annotations

import re
from pathlib import Path

from ssios.errors import SsiosError
from ssios.library import import_path, metadata, replace_metadata
from ssios.locale_messages import say
from ssios.util import read_json, write_json



def local_directory(path: str, state_dir: Path, lang: str = "en") -> Path:
    """A real directory on this machine. Network paths and the state directory are refused."""
    from ssios.util import real_directory

    resolved, why = real_directory(path, Path(state_dir))
    if resolved is None:
        raise SsiosError("library_watch", say(lang, "watch_rejected") + " " + why, 400)
    return resolved


def set_watch(plane, account_id: str, library_id: str, path: str, enabled: bool, peer_ip: str, *, delete_source: bool = False) -> dict:
    from ssios.library import _audit, _lang, _require

    _require(plane, account_id, library_id, "organize")
    lang = _lang(plane, account_id)
    stored = ""
    if str(path or "").strip():
        stored = str(local_directory(path, plane.settings.state_dir, lang))
    if enabled and not stored:
        raise SsiosError("library_watch", say(lang, "watch_rejected"), 400)
    data = metadata(plane, account_id, library_id)
    data["watch"] = {"enabled": bool(enabled), "path": stored, "delete_source": bool(delete_source)}
    replace_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.watch", library_id, "on" if enabled else "off", 0)
    return data["watch"]


def scan_watch(plane, account_id: str, library_id: str, peer_ip: str) -> dict:
    """Import a file only after its size and time match the previous look. The source stays unless asked."""
    from ssios.library import _lang, _library_root

    lang = _lang(plane, account_id)
    data = metadata(plane, account_id, library_id)
    watch = data.get("watch") if isinstance(data.get("watch"), dict) else {}
    report = {"enabled": bool(watch.get("enabled")), "imported": [], "waiting": [], "rejected": []}
    if not watch.get("enabled"):
        return report
    root = local_directory(str(watch.get("path") or ""), plane.settings.state_dir, lang)
    seen_path = _library_root(plane, account_id, library_id) / "watch-seen.json"
    seen = read_json(seen_path, {})
    if not isinstance(seen, dict):
        seen = {}
    fresh: dict[str, dict] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part.startswith(".") or part == ".git" for part in Path(relative).parts):
            report["rejected"].append({"path": relative, "reason": say(lang, "library_rejected")})
            continue
        stat = path.stat()
        mark = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        previous = seen.get(relative) if isinstance(seen.get(relative), dict) else None
        fresh[relative] = mark
        if previous != mark:
            report["waiting"].append({"path": relative, "reason": say(lang, "watch_waiting")})
            continue
        try:
            saved = import_path(
                plane,
                account_id,
                library_id,
                path,
                name=path.name,
                source="drag",
                peer_ip=peer_ip,
            )
        except SsiosError as exc:
            report["rejected"].append({"path": relative, "reason": exc.detail})
            continue
        if saved.get("result") == "conflict":
            report["rejected"].append({"path": relative, "reason": saved.get("reason") or ""})
            continue
        report["imported"].append({"path": relative, "id": saved.get("id") or ""})
        if watch.get("delete_source"):
            path.unlink()
    write_json(seen_path, fresh)
    return report

"""Copy an Eagle library into Files. The Eagle folder is only read. Nothing in it is written, moved, or deleted.

Layout read here, as importers and the plugin notes describe it for Eagle 3 and 4:

    Name.library/metadata.json            folders (a tree), smartFolders, tagsGroups
    Name.library/images/<ID>.info/metadata.json
    Name.library/images/<ID>.info/<name>.<ext>            the original
    Name.library/images/<ID>.info/<name>_thumbnail.png    not copied

Eagle does not publish this format. The tests use a synthetic library built to this layout. A field
that is missing or has another shape is skipped and counted, not guessed.

Each file goes through the same rules as any other import: the kinds Files stores, the size limits,
and the removal of location and device data from images. Each item keeps its Eagle id, times, and
palette, and the smart folders are kept as Eagle wrote them, so a later export to Eagle has what it needs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ssios.errors import SsiosError

_MAX_JSON = 4_000_000


def _load(path: Path) -> dict:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_JSON:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _stamp(value, fallback: str) -> str:
    """Eagle stores milliseconds since 1970."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number <= 0:
        return fallback
    try:
        return datetime.fromtimestamp(number / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()
    except (OverflowError, OSError, ValueError):
        return fallback


def eagle_source(plane, account_id: str, source: str) -> Path:
    """A local folder that looks like an Eagle library."""
    from ssios.library import _lang
    from ssios.library_watch import local_directory

    try:
        root = local_directory(str(source or "").rstrip("/"), plane.settings.state_dir, _lang(plane, account_id))
    except SsiosError as exc:
        raise SsiosError("eagle_source", "That is not a folder on this computer or a connected disk.", 400) from exc
    if not (root / "metadata.json").is_file() or not (root / "images").is_dir():
        raise SsiosError("eagle_source", "That folder is not an Eagle library. It needs metadata.json and an images folder.", 400)
    return root


def _folder_rows(nodes, parent: str, library_id: str, known: dict[str, str], rows: list[dict], depth: int = 0) -> None:
    if not isinstance(nodes, list) or depth > 12:
        return
    for node in nodes:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        eagle_id = str(node["id"])
        if eagle_id not in known:
            folder_id = "fld-" + hashlib.sha256(f"{library_id}:{eagle_id}".encode()).hexdigest()[:8]
            title = " ".join(str(node.get("name") or "").replace("/", "-").split()).lstrip(".")[:40] or "Folder"
            known[eagle_id] = folder_id
            rows.append({"id": folder_id, "name": title, "parent_id": parent, "eagle_id": eagle_id})
        _folder_rows(node.get("children"), known[eagle_id], library_id, known, rows, depth + 1)


def _original(info_dir: Path, meta: dict) -> Path | None:
    name = str(meta.get("name") or "")
    ext = str(meta.get("ext") or "")
    wanted = info_dir / f"{name}.{ext}"
    if name and ext and "/" not in name and wanted.is_file() and not wanted.is_symlink():
        return wanted
    best = None
    try:
        for entry in info_dir.iterdir():
            if entry.is_symlink() or not entry.is_file() or entry.name == "metadata.json" or entry.name.endswith("_thumbnail.png"):
                continue
            if best is None or entry.stat().st_size > best.stat().st_size:
                best = entry
    except OSError:
        return None
    return best


def _tags(values) -> list[str]:
    found = []
    for value in values if isinstance(values, list) else []:
        title = " ".join(str(value).split())[:40]
        if title and "/" not in title and title not in found:
            found.append(title)
    return found[:40]


def import_eagle(plane, account_id: str, source: str, peer_ip: str, *, location: str = "", library_id: str = "", progress=None) -> dict:
    """Copy the library in. Running it again on the same folder continues: items already copied are skipped."""
    from ssios.library import (
        _captured,
        _check_name,
        _classify,
        _clean,
        _items,
        _lang,
        _library_root,
        _limit_for,
        _metadata,
        _publish_incoming,
        _put_blob,
        _require,
        _write_incoming,
        _write_item,
        _write_metadata,
        create_library,
        list_libraries,
    )

    root = eagle_source(plane, account_id, source)
    lang = _lang(plane, account_id)
    head = _load(root / "metadata.json")
    if not library_id:
        for row in list_libraries(plane, account_id):
            if row.get("offline") or row.get("role") != "owner":
                continue
            marked = _metadata(plane, account_id, row["id"], create=False, name="").get("eagle_source")
            if isinstance(marked, dict) and marked.get("path") == str(root):
                library_id = row["id"]
                break
    created = False
    if not library_id:
        name = root.name[: -len(".library")] if root.name.endswith(".library") else root.name
        library_id = create_library(plane, account_id, name, peer_ip, location)["id"]
        created = True
    from ssios.library import _owned

    _owned(plane, account_id, library_id)
    _require(plane, account_id, library_id, "owner")
    data = _metadata(plane, account_id, library_id, create=False, name="")
    known = {str(key): str(value) for key, value in (data.get("eagle_folders") or {}).items()} if isinstance(data.get("eagle_folders"), dict) else {}
    new_folders: list[dict] = []
    _folder_rows(head.get("folders"), "", library_id, known, new_folders)
    data["folders"] = list(data.get("folders") or []) + new_folders
    data["eagle_folders"] = known
    data["eagle_source"] = {"path": str(root), "application_version": str(head.get("applicationVersion") or "")[:40]}
    groups = []
    for group in head.get("tagsGroups") if isinstance(head.get("tagsGroups"), list) else []:
        if isinstance(group, dict) and group.get("name"):
            groups.append({"id": str(group.get("id") or "")[:40], "name": " ".join(str(group["name"]).split())[:40], "tags": _tags(group.get("tags")), "color": str(group.get("color") or "")[:20]})
    data["tag_groups"] = groups[:200]
    smart = head.get("smartFolders") if isinstance(head.get("smartFolders"), list) else []
    # Kept as Eagle wrote them. Their rules are not translated into Files smart folders.
    data["eagle_smart_folders"] = smart if len(json.dumps(smart)) <= 400_000 else []
    _write_metadata(plane, account_id, library_id, data)

    existing = _items(plane, account_id, library_id)
    by_eagle = {str(item.get("eagle_id")): item for item in existing if item.get("eagle_id")}
    for item in existing:
        for other in item.get("eagle_also") or []:
            by_eagle[str(other)] = item
    by_digest = {str(item.get("sha256")): item for item in existing}
    ids = {str(item.get("id")) for item in existing}
    report = {"library_id": library_id, "created": created, "imported": 0, "already": 0, "duplicates": 0, "deleted": 0, "skipped": {}, "skipped_kinds": {}, "bytes": 0}
    all_tags = [str(tag) for tag in data.get("tags") or []]

    def skip(reason: str, ext: str = "") -> None:
        report["skipped"][reason] = report["skipped"].get(reason, 0) + 1
        if ext:
            report["skipped_kinds"][ext] = report["skipped_kinds"].get(ext, 0) + 1

    try:
        infos = sorted(entry for entry in (root / "images").iterdir() if entry.name.endswith(".info") and entry.is_dir() and not entry.is_symlink())
    except OSError as exc:
        raise SsiosError("eagle_source", "The images folder could not be read.", 400) from exc
    stamp_now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for index, info_dir in enumerate(infos, start=1):
        if progress is not None and (index % 50 == 0 or index == len(infos)):
            progress(index, len(infos), report)
        meta = _load(info_dir / "metadata.json")
        eagle_id = str(meta.get("id") or info_dir.name[: -len(".info")])
        if not meta:
            skip("unreadable")
            continue
        if meta.get("isDeleted"):
            report["deleted"] += 1
            continue
        if eagle_id in by_eagle:
            report["already"] += 1
            continue
        original = _original(info_dir, meta)
        ext_hint = str(meta.get("ext") or "").casefold()[:12]
        if original is None:
            skip("no file", ext_hint)
            continue
        try:
            try:
                filename = _check_name(original.name, lang)
            except SsiosError:
                filename = _check_name(f"{eagle_id}.{ext_hint or 'bin'}", lang)
            size = original.stat().st_size
            if size <= 0:
                skip("empty", ext_hint)
                continue
            with original.open("rb") as handle:
                kind = _classify(filename, handle.read(80_000), lang)
            if size > _limit_for(kind):
                skip("too large", ext_hint)
                continue
            captured = ""
            if kind["ext"] in {"png", "jpg", "gif", "webp"}:
                raw = original.read_bytes()
                captured = _captured(raw)
                stored = _clean(raw, kind)
                digest = hashlib.sha256(stored).hexdigest()
                stored_size = len(stored)
                if digest not in by_digest:
                    _put_blob(plane, account_id, library_id, digest, kind["ext"], stored)
            else:
                digest, stored_size, incoming = _write_incoming(plane, account_id, library_id, original)
                _publish_incoming(plane, account_id, library_id, incoming, digest, kind["ext"])
        except SsiosError:
            skip("kind not stored", ext_hint)
            continue
        except OSError:
            skip("read error", ext_hint)
            continue
        tags = _tags(meta.get("tags"))
        folders = [known[str(one)] for one in meta.get("folders") if str(one) in known] if isinstance(meta.get("folders"), list) else []
        for tag in tags:
            if tag not in all_tags:
                all_tags.append(tag)
        twin = by_digest.get(digest)
        if twin is not None:
            # The same bytes are stored once. The second Eagle item adds its tags and folders to the first.
            twin["tags"] = (list(twin.get("tags") or []) + [tag for tag in tags if tag not in (twin.get("tags") or [])])[:40]
            twin["folder_ids"] = list(twin.get("folder_ids") or []) + [one for one in folders if one not in (twin.get("folder_ids") or [])]
            twin["eagle_also"] = (list(twin.get("eagle_also") or []) + [eagle_id])[:50]
            _write_item(plane, account_id, library_id, twin)
            by_eagle[eagle_id] = twin
            report["duplicates"] += 1
            continue
        item_id = "item-" + digest[:8]
        while item_id in ids:
            item_id = "item-" + hashlib.sha256(item_id.encode()).hexdigest()[:8]
        try:
            star = max(0, min(5, int(meta.get("star") or 0)))
        except (TypeError, ValueError):
            star = 0
        imported_at = _stamp(meta.get("btime"), stamp_now)
        item = {
            "id": item_id,
            "library_id": library_id,
            "name": filename,
            "ext": kind["ext"],
            "mime": kind["mime"],
            "group": kind["group"],
            "size": stored_size,
            "sha256": digest,
            "folder_ids": folders,
            "tags": tags,
            "star": star,
            "annotation": " ".join(str(meta.get("annotation") or "").split())[:240],
            "source_kind": "eagle",
            "source_url": str(meta.get("url") or "")[:500] if str(meta.get("url") or "").startswith(("http://", "https://")) else "",
            "imported_at": imported_at,
            "modified_at": _stamp(meta.get("modificationTime") or meta.get("mtime"), imported_at),
            "captured_at": captured,
            "added_by": account_id,
            "versions": [{"sha256": digest, "size": stored_size, "imported_at": imported_at}],
            "trashed": False,
            "trashed_at": "",
            "eagle_id": eagle_id,
            "eagle": {
                "btime": meta.get("btime") if isinstance(meta.get("btime"), (int, float)) else None,
                "mtime": meta.get("mtime") if isinstance(meta.get("mtime"), (int, float)) else None,
                "width": meta.get("width") if isinstance(meta.get("width"), int) else None,
                "height": meta.get("height") if isinstance(meta.get("height"), int) else None,
                "palettes": meta.get("palettes")[:12] if isinstance(meta.get("palettes"), list) and len(json.dumps(meta.get("palettes")[:12])) < 4000 else [],
                "note": str(meta.get("annotation") or "")[:4000],
            },
        }
        _write_item(plane, account_id, library_id, item)
        ids.add(item_id)
        by_digest[digest] = item
        by_eagle[eagle_id] = item
        report["imported"] += 1
        report["bytes"] += stored_size
    data = _metadata(plane, account_id, library_id, create=False, name="")
    data["tags"] = all_tags[:2000]
    _write_metadata(plane, account_id, library_id, data)
    report["folders"] = len(known)
    report["tags"] = len(all_tags)
    report["smart_folders_not_translated"] = len(smart)
    report["location"] = str(_library_root(plane, account_id, library_id))
    plane.security_audit.write(
        actor=account_id,
        ip=peer_ip or "127.0.0.1",
        action="library.eagle_import",
        target=library_id,
        result="ok",
        extra={"service": "library", "item_count": str(report["imported"]), "byte_count": str(report["bytes"])},
    )
    return report

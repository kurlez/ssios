"""Personal asset libraries. Originals are immutable copies under the state directory.

Chat files stay where they are. The first visit copies them into the default
personal library. A matching name or the same bytes waits for a choice. The
same hash is stored once.
"""

from __future__ import annotations

import hashlib
import re
import struct
import threading
import zlib
from datetime import datetime, timezone

from ssios.errors import SsiosError
from ssios.locale_messages import say
from ssios.util import read_json, secure_mkdir, write_json

IMAGE_LIMIT = 50_000_000
DOCUMENT_LIMIT = 100_000_000
MEDIA_LIMIT = 500_000_000
_LOCK = threading.Lock()
_PARTIAL = {".part", ".crdownload", ".download", ".tmp", ".filepart"}
_ARCHIVE = {".zip", ".7z", ".rar", ".gz", ".tgz", ".dmg", ".iso", ".tar"}
_ID = re.compile(r"lib-[0-9a-f]{8}")
_ITEM = re.compile(r"item-[0-9a-f]{8}")
_FOLDER = re.compile(r"fld-[0-9a-f]{8}")


def ensure_personal(plane, account_id: str) -> dict:
    """The account's default personal library, creating it on first use."""
    home = plane.accounts.home(account_id)
    root = home / "library"
    secure_mkdir(root)
    index_path = root / "libraries.json"
    index = read_json(index_path, {"libraries": [], "files_migrated": False})
    libraries = index.get("libraries") if isinstance(index.get("libraries"), list) else []
    current = next((row for row in libraries if isinstance(row, dict) and row.get("kind") == "personal"), None)
    if current is None:
        library_id = "lib-" + hashlib.sha256(account_id.encode()).hexdigest()[:8]
        current = {"id": library_id, "name": "Personal", "kind": "personal"}
        libraries.insert(0, current)
        index["libraries"] = libraries
        write_json(index_path, index)
        _metadata(plane, account_id, library_id, create=True, name="Personal")
    if not index.get("files_migrated"):
        index["files_migrated"] = True
        index["libraries"] = libraries
        write_json(index_path, index)
        _migrate_files(plane, account_id, str(current["id"]))
    return current


def list_libraries(plane, account_id: str) -> list[dict]:
    ensure_personal(plane, account_id)
    index = read_json(plane.accounts.home(account_id) / "library" / "libraries.json", {})
    rows = []
    for row in index.get("libraries") or []:
        if isinstance(row, dict) and _ID.fullmatch(str(row.get("id") or "")):
            rows.append({"id": row["id"], "name": str(row.get("name") or "Files"), "kind": "personal"})
    return rows


def create_library(plane, account_id: str, name: str, peer_ip: str) -> dict:
    ensure_personal(plane, account_id)
    title = " ".join(str(name or "").split())[:40]
    if len(title) < 2:
        title = "Files"
    library_id = "lib-" + hashlib.sha256(f"{account_id}:{title}:{_now()}".encode()).hexdigest()[:8]
    index_path = plane.accounts.home(account_id) / "library" / "libraries.json"
    with _LOCK:
        index = read_json(index_path, {"libraries": []})
        while any(isinstance(row, dict) and row.get("id") == library_id for row in index.get("libraries") or []):
            library_id = "lib-" + hashlib.sha256(library_id.encode()).hexdigest()[:8]
        row = {"id": library_id, "name": title, "kind": "personal"}
        index.setdefault("libraries", []).append(row)
        write_json(index_path, index)
    _metadata(plane, account_id, library_id, create=True, name=title)
    _audit(plane, account_id, peer_ip, "library.create", library_id, "ok", 0)
    return row


def library_or_default(plane, account_id: str, library_id: str) -> dict:
    rows = list_libraries(plane, account_id)
    for row in rows:
        if row["id"] == library_id:
            return row
    return rows[0]


def metadata(plane, account_id: str, library_id: str) -> dict:
    _owned(plane, account_id, library_id)
    return _metadata(plane, account_id, library_id, create=False, name="")


def replace_metadata(plane, account_id: str, library_id: str, data: dict) -> None:
    """Replace the catalog header for a library this account can open."""
    _owned(plane, account_id, library_id)
    _write_metadata(plane, account_id, library_id, data)


def _owned(plane, account_id: str, library_id: str) -> dict:
    chosen = library_or_default(plane, account_id, library_id)
    if chosen["id"] != library_id:
        raise SsiosError("library_missing", say(_lang(plane, account_id), "library_missing"), 404)
    return chosen


def add_folder(plane, account_id: str, library_id: str, name: str, peer_ip: str, parent_id: str = "") -> dict:
    title = " ".join(str(name or "").split())[:40]
    if len(title) < 1 or "/" in title or title.startswith("."):
        raise SsiosError("library_rejected", say(_lang(plane, account_id), "library_rejected"), 400)
    if parent_id and not _FOLDER.fullmatch(parent_id):
        raise SsiosError("library_rejected", say(_lang(plane, account_id), "library_rejected"), 400)
    folder_id = "fld-" + hashlib.sha256(f"{library_id}:{parent_id}:{title}:{_now()}".encode()).hexdigest()[:8]
    data = _metadata(plane, account_id, library_id, create=False, name="")
    data.setdefault("folders", []).append({"id": folder_id, "name": title, "parent_id": parent_id or ""})
    _write_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.folder", folder_id, "ok", 0)
    return {"id": folder_id, "name": title}


def add_tag(plane, account_id: str, library_id: str, name: str, peer_ip: str) -> str:
    title = " ".join(str(name or "").split())[:40]
    if not title:
        raise SsiosError("library_rejected", say(_lang(plane, account_id), "library_rejected"), 400)
    data = _metadata(plane, account_id, library_id, create=False, name="")
    tags = [str(tag) for tag in data.get("tags") or []]
    if title not in tags:
        tags.append(title)
        data["tags"] = tags[:200]
        _write_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.tag", library_id, "ok", 0)
    return title


def import_bytes(
    plane,
    account_id: str,
    library_id: str,
    raw: bytes,
    *,
    name: str,
    source: str,
    peer_ip: str,
    folder_id: str = "",
) -> dict:
    """Store one finished file. A name or byte conflict is staged until the owner chooses."""
    lang = _lang(plane, account_id)
    library_or_default(plane, account_id, library_id)
    filename = _check_name(name, lang)
    if not isinstance(raw, bytes) or not raw:
        raise SsiosError("library_empty", say(lang, "library_empty"), 400)
    kind = _classify(filename, raw, lang)
    limit = _limit_for(kind)
    if len(raw) > limit:
        raise SsiosError("library_too_large", say(lang, "library_too_large", limit=limit), 400)
    captured = _captured(raw) if kind["group"] == "image" else ""
    stored = _clean(raw, kind)
    digest = hashlib.sha256(stored).hexdigest()
    conflict = _find_conflict(plane, account_id, library_id, filename, digest, folder_id)
    if conflict is not None:
        return _stage_conflict(plane, account_id, library_id, stored, conflict, filename, kind, captured, source, folder_id)
    _put_blob(plane, account_id, library_id, digest, kind["ext"], stored)
    return _record_item(
            plane,
            account_id,
            library_id,
            filename=filename,
            kind=kind,
            digest=digest,
            size=len(stored),
            captured=captured,
            source=source,
            peer_ip=peer_ip,
            folder_id=folder_id,
        )


def import_path(
    plane,
    account_id: str,
    library_id: str,
    path,
    *,
    name: str,
    source: str,
    peer_ip: str,
    folder_id: str = "",
) -> dict:
    """Import a file already on disk. The body is hashed in chunks, not held as one buffer."""
    from pathlib import Path

    lang = _lang(plane, account_id)
    library_or_default(plane, account_id, library_id)
    filename = _check_name(name, lang)
    src = Path(path)
    if not src.is_file() or src.is_symlink():
        raise SsiosError("library_empty", say(lang, "library_empty"), 400)
    size = src.stat().st_size
    if size <= 0:
        raise SsiosError("library_empty", say(lang, "library_empty"), 400)
    with src.open("rb") as handle:
        header = handle.read(80_000)
    kind = _classify(filename, header, lang)
    limit = _limit_for(kind)
    if size > limit:
        raise SsiosError("library_too_large", say(lang, "library_too_large", limit=limit), 400)
    if kind["ext"] in {"png", "jpg", "gif", "webp"}:
        return import_bytes(
            plane,
            account_id,
            library_id,
            src.read_bytes(),
            name=filename,
            source=source,
            peer_ip=peer_ip,
            folder_id=folder_id,
        )
    digest, stored_size, incoming = _write_incoming(plane, account_id, library_id, src)
    conflict = _find_conflict(plane, account_id, library_id, filename, digest, folder_id)
    if conflict is not None:
        if conflict["conflict_kind"] == "identical":
            incoming.unlink(missing_ok=True)
            return _stage_conflict(plane, account_id, library_id, None, conflict, filename, kind, "", source, folder_id, digest=digest, size=stored_size)
        return _stage_conflict(plane, account_id, library_id, None, conflict, filename, kind, "", source, folder_id, digest=digest, size=stored_size, incoming=incoming)
    _publish_incoming(plane, account_id, library_id, incoming, digest, kind["ext"])
    return _record_item(
        plane,
        account_id,
        library_id,
        filename=filename,
        kind=kind,
        digest=digest,
        size=stored_size,
        captured="",
        source=source,
        peer_ip=peer_ip,
        folder_id=folder_id,
    )


def import_relative(
    plane,
    account_id: str,
    library_id: str,
    path,
    *,
    relative: str,
    source: str,
    peer_ip: str,
    folder_id: str = "",
) -> dict:
    """Import one dropped file. A directory path is rejected and is not walked."""
    lang = _lang(plane, account_id)
    parts = [part for part in str(relative or "").replace("\\", "/").split("/") if part not in {"", "."}]
    shown = "/".join(parts) or str(relative or "")
    if len(parts) > 1 or shown.endswith("/"):
        return {"path": shown.rstrip("/"), "status": "rejected", "code": "drop_directory", "reason": say(lang, "drop_directory"), "id": ""}
    if not parts or any(part in {"..", ".git"} or part.startswith(".") for part in parts):
        return {"path": shown, "status": "rejected", "code": "library_rejected", "reason": say(lang, "library_rejected"), "id": ""}
    try:
        parent = folder_id or ""
        saved = import_path(
            plane,
            account_id,
            library_id,
            path,
            name=parts[-1],
            source=source,
            peer_ip=peer_ip,
            folder_id=parent,
        )
    except SsiosError as exc:
        return {"path": shown, "status": "rejected", "code": exc.code, "reason": exc.detail, "id": ""}
    if saved.get("result") == "conflict":
        return {
            "path": shown,
            "status": "conflict",
            "code": saved.get("conflict_kind") or "",
            "reason": saved.get("reason") or "",
            "id": saved.get("id") or "",
            "conflict_id": saved.get("conflict_id") or "",
            "default": saved.get("default") or "",
        }
    status = "version" if saved.get("result") == "version" else "saved"
    reason = say(lang, "drop_version" if status == "version" else "drop_saved")
    return {"path": shown, "status": status, "code": "", "reason": reason, "id": saved.get("id") or ""}


def drop_summary(lang: str, results: list[dict]) -> str:
    saved = sum(1 for row in results if row.get("status") == "saved")
    versions = sum(1 for row in results if row.get("status") == "version")
    skipped = sum(1 for row in results if row.get("status") in {"rejected", "kept"})
    return say(lang, "drop_summary", saved=saved, versions=versions, skipped=skipped)


def list_items(plane, account_id: str, library_id: str, *, folder: str = "", tag: str = "", trash: bool = False, uncategorized: bool = False, untagged: bool = False) -> list[dict]:
    library_or_default(plane, account_id, library_id)
    rows = []
    for item in _items(plane, account_id, library_id):
        if bool(item.get("trashed")) != trash:
            continue
        folders = item.get("folder_ids") if isinstance(item.get("folder_ids"), list) else []
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        if folder and folder not in folders:
            continue
        if tag and tag not in tags:
            continue
        if uncategorized and folders:
            continue
        if untagged and tags:
            continue
        rows.append(item)
    return rows


def get_item(plane, account_id: str, library_id: str, item_id: str) -> dict:
    if not _ITEM.fullmatch(item_id):
        raise SsiosError("library_missing", say(_lang(plane, account_id), "library_missing"), 404)
    for item in _items(plane, account_id, library_id):
        if item.get("id") == item_id:
            return item
    raise SsiosError("library_missing", say(_lang(plane, account_id), "library_missing"), 404)


def item_bytes(plane, account_id: str, library_id: str, item_id: str) -> tuple[bytes, str]:
    item = get_item(plane, account_id, library_id, item_id)
    path = _library_root(plane, account_id, library_id) / "objects" / f"{item['sha256']}.{item['ext']}"
    if not path.is_file() or path.is_symlink():
        raise SsiosError("library_missing", say(_lang(plane, account_id), "library_missing"), 404)
    return path.read_bytes(), str(item.get("mime") or "application/octet-stream")


def update_item(plane, account_id: str, library_id: str, item_id: str, *, peer_ip: str, folder_id: str = "", tag: str = "", star: int | None = None, annotation: str | None = None, trash: bool | None = None, restore: bool = False) -> dict:
    item = get_item(plane, account_id, library_id, item_id)
    action = "library.update"
    if folder_id:
        if not _FOLDER.fullmatch(folder_id):
            raise SsiosError("library_rejected", say(_lang(plane, account_id), "library_rejected"), 400)
        folders = list(item.get("folder_ids") or [])
        if folder_id not in folders:
            folders.append(folder_id)
        item["folder_ids"] = folders
        action = "library.folder"
    if tag:
        title = " ".join(tag.split())[:40]
        tags = list(item.get("tags") or [])
        if title and title not in tags:
            tags.append(title)
        item["tags"] = tags
        action = "library.tag"
    if star is not None:
        item["star"] = max(0, min(5, int(star)))
    if annotation is not None:
        item["annotation"] = " ".join(str(annotation).split())[:240]
    if trash:
        item["trashed"] = True
        item["trashed_at"] = _now()
        action = "library.trash"
    if restore:
        item["trashed"] = False
        item["trashed_at"] = ""
        action = "library.restore"
    item["modified_at"] = _now()
    _write_item(plane, account_id, library_id, item)
    _audit(plane, account_id, peer_ip, action, item_id, "ok", int(item.get("size") or 0))
    return item


def purge_item(plane, account_id: str, library_id: str, item_id: str, confirm: str, peer_ip: str) -> None:
    if confirm != "delete":
        raise SsiosError("library_rejected", say(_lang(plane, account_id), "library_rejected"), 400)
    item = get_item(plane, account_id, library_id, item_id)
    if not item.get("trashed"):
        raise SsiosError("library_rejected", say(_lang(plane, account_id), "library_rejected"), 400)
    path = _item_path(plane, account_id, library_id, item_id)
    if path.is_file() and not path.is_symlink():
        path.unlink()
    cited = set()
    for other in _items(plane, account_id, library_id):
        if other.get("id") == item_id:
            continue
        cited.add(str(other.get("sha256") or ""))
        for version in other.get("versions") or []:
            if isinstance(version, dict):
                cited.add(str(version.get("sha256") or ""))
    for version in item.get("versions") or []:
        digest = str(version.get("sha256") or "") if isinstance(version, dict) else ""
        if digest and digest not in cited:
            blob = _library_root(plane, account_id, library_id) / "objects" / f"{digest}.{item.get('ext')}"
            if blob.is_file() and not blob.is_symlink():
                blob.unlink()
    _audit(plane, account_id, peer_ip, "library.purge", item_id, "ok", int(item.get("size") or 0))


def _migrate_files(plane, account_id: str, library_id: str) -> None:
    from ssios.files import FileLibrary

    files = FileLibrary(plane)
    try:
        rows = files.list(account_id)
    except SsiosError:
        return
    for row in rows:
        try:
            raw, _kind, name = files.bytes(account_id, str(row.get("id") or ""))
        except SsiosError:
            continue
        try:
            import_bytes(plane, account_id, library_id, raw, name=name, source="picker", peer_ip="127.0.0.1")
        except SsiosError:
            continue


def _classify(filename: str, raw: bytes, lang: str) -> dict:
    lower = filename.casefold()
    suffix = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    if suffix in _ARCHIVE or raw.startswith((b"Rar!", b"7z\xbc\xaf", b"\x1f\x8b")):
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    if raw.startswith((b"MZ", b"\x7fELF", b"\xfe\xed\xfa", b"\xcf\xfa\xed\xfe")) or raw.startswith(b"SQLite format 3"):
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return {"ext": "png", "mime": "image/png", "group": "image"}
    if raw.startswith(b"\xff\xd8\xff"):
        return {"ext": "jpg", "mime": "image/jpeg", "group": "image"}
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return {"ext": "gif", "mime": "image/gif", "group": "image"}
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return {"ext": "webp", "mime": "image/webp", "group": "image"}
    if raw.startswith(b"RIFF") and raw[8:12] == b"WAVE":
        return {"ext": "wav", "mime": "audio/wav", "group": "audio"}
    if raw.startswith(b"BM"):
        return {"ext": "bmp", "mime": "image/bmp", "group": "image"}
    if raw.startswith((b"II*\x00", b"MM\x00*")):
        return {"ext": "tif", "mime": "image/tiff", "group": "image"}
    if raw.startswith(b"%PDF-"):
        return {"ext": "pdf", "mime": "application/pdf", "group": "document"}
    if raw.startswith(b"8BPS"):
        return {"ext": "psd", "mime": "image/vnd.adobe.photoshop", "group": "design"}
    if raw.startswith((b"\x00\x01\x00\x00", b"OTTO")):
        return {"ext": "otf" if raw.startswith(b"OTTO") else "ttf", "mime": "font/otf" if raw.startswith(b"OTTO") else "font/ttf", "group": "font"}
    if raw.startswith((b"wOFF", b"wOF2")):
        return {"ext": "woff2" if raw.startswith(b"wOF2") else "woff", "mime": "font/woff", "group": "font"}
    if raw.startswith(b"ID3") or raw.startswith(b"fLaC") or raw.startswith(b"OggS"):
        ext = "mp3" if raw.startswith(b"ID3") else "flac" if raw.startswith(b"fLaC") else "ogg"
        return {"ext": ext, "mime": "audio/" + ext, "group": "audio"}
    if len(raw) > 12 and raw[4:8] == b"ftyp":
        brand = raw[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"mif1", b"msf1"}:
            return {"ext": "heic", "mime": "image/heic", "group": "image"}
        if brand == b"avif":
            return {"ext": "avif", "mime": "image/avif", "group": "image"}
        if brand in {b"M4A ", b"mp4a"}:
            return {"ext": "m4a", "mime": "audio/mp4", "group": "audio"}
        return {"ext": "mp4" if brand != b"qt  " else "mov", "mime": "video/mp4", "group": "video"}
    if raw.startswith(b"PK\x03\x04"):
        head = raw[:80_000]
        if b"word/document.xml" in head:
            return {"ext": "docx", "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "group": "document"}
        if b"ppt/presentation.xml" in head or b"ppt/slides/" in head:
            return {"ext": "pptx", "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "group": "document"}
        if b"xl/workbook.xml" in head:
            return {"ext": "xlsx", "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "group": "document"}
        if b"application/epub+zip" in head:
            return {"ext": "epub", "mime": "application/epub+zip", "group": "document"}
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    raise SsiosError("library_type", say(lang, "library_type"), 400)


def _check_name(name: str, lang: str) -> str:
    base = str(name or "").replace("\\", "/").split("/")[-1].strip()
    parts = [part for part in str(name or "").replace("\\", "/").split("/") if part]
    if any(part == ".git" for part in parts) or base in {".gitignore", ".git"}:
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    if not base or base.startswith(".") or base.casefold() == ".ds_store":
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    suffix = "." + base.casefold().rsplit(".", 1)[-1] if "." in base else ""
    if suffix in _PARTIAL or base.casefold().endswith(".download"):
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    cleaned = re.sub(r"[\x00-\x1f]", "", base).strip(" .")[:120]
    if not cleaned:
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    return cleaned


def _clean(raw: bytes, kind: dict) -> bytes:
    if kind["group"] == "image" and kind["ext"] in {"png", "jpg", "gif", "webp"}:
        from ssios.files import _strip

        return _strip(raw)
    return raw


def _captured(raw: bytes) -> str:
    window = raw[:65536]
    found = re.search(br"(20\d{2}:\d{2}:\d{2} \d{2}:\d{2}:\d{2})", window)
    if not found:
        return ""
    start = max(0, found.start() - 80)
    nearby = window[start:found.end()]
    if b"DateTime" not in nearby:
        return ""
    return found.group(1).decode("ascii")


def _limit_for(kind: dict) -> int:
    return {"image": IMAGE_LIMIT, "video": MEDIA_LIMIT, "audio": MEDIA_LIMIT}.get(kind["group"], DOCUMENT_LIMIT)


def _record_item(
    plane,
    account_id: str,
    library_id: str,
    *,
    filename: str,
    kind: dict,
    digest: str,
    size: int,
    captured: str,
    source: str,
    peer_ip: str,
    folder_id: str,
    existing_id: str = "",
) -> dict:
    stamp = _now()
    with _LOCK:
        items = _items(plane, account_id, library_id)
        match = next((item for item in items if item.get("id") == existing_id), None) if existing_id else None
        if existing_id and match is None:
            raise SsiosError("library_missing", say(_lang(plane, account_id), "library_missing"), 404)
        version = {"sha256": digest, "size": size, "imported_at": stamp}
        folders = [folder_id] if folder_id else []
        if match is None:
            item_id = "item-" + digest[:8]
            while any(item.get("id") == item_id for item in items):
                item_id = "item-" + hashlib.sha256(item_id.encode()).hexdigest()[:8]
            match = {
                "id": item_id,
                "library_id": library_id,
                "name": filename,
                "ext": kind["ext"],
                "mime": kind["mime"],
                "group": kind["group"],
                "size": size,
                "sha256": digest,
                "folder_ids": folders,
                "tags": [],
                "star": 0,
                "annotation": "",
                "source_kind": source if source in {"paste", "drag", "picker"} else "picker",
                "imported_at": stamp,
                "modified_at": stamp,
                "captured_at": captured,
                "added_by": account_id,
                "versions": [version],
                "trashed": False,
                "trashed_at": "",
            }
            _write_item(plane, account_id, library_id, match)
            action = "library.import"
            result = "saved"
        else:
            match["versions"] = list(match.get("versions") or []) + [version]
            match["sha256"] = digest
            match["size"] = size
            match["ext"] = kind["ext"]
            match["mime"] = kind["mime"]
            match["group"] = kind["group"]
            match["modified_at"] = stamp
            if captured:
                match["captured_at"] = captured
            if folder_id and folder_id not in (match.get("folder_ids") or []):
                match["folder_ids"] = list(match.get("folder_ids") or []) + [folder_id]
            _write_item(plane, account_id, library_id, match)
            action = "library.version"
            result = "version"
    _audit(plane, account_id, peer_ip, action, match["id"], result, size)
    public = dict(match)
    public["version_count"] = len(match.get("versions") or [])
    public["result"] = result
    return public


_CHUNK = 1024 * 1024


def _write_incoming(plane, account_id: str, library_id: str, src):
    import secrets
    from pathlib import Path

    folder = _library_root(plane, account_id, library_id) / "objects"
    secure_mkdir(folder)
    incoming = folder / f".incoming-{secrets.token_hex(8)}"
    digest = hashlib.sha256()
    size = 0
    with Path(src).open("rb") as handle, incoming.open("wb") as out:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
            size += len(chunk)
    incoming.chmod(0o600)
    return digest.hexdigest(), size, incoming


def _publish_incoming(plane, account_id: str, library_id: str, incoming, digest: str, ext: str) -> None:
    final = _library_root(plane, account_id, library_id) / "objects" / f"{digest}.{ext}"
    if final.exists():
        incoming.unlink(missing_ok=True)
        return
    incoming.replace(final)
    final.chmod(0o600)


def _find_conflict(plane, account_id: str, library_id: str, filename: str, digest: str, folder_id: str):
    items = [item for item in _items(plane, account_id, library_id) if not item.get("trashed")]
    identical = next((item for item in items if item.get("sha256") == digest), None)
    if identical is not None:
        return {"item": identical, "conflict_kind": "identical"}
    named = next(
        (
            item
            for item in items
            if str(item.get("name") or "").casefold() == filename.casefold() and _same_folder(item, folder_id)
        ),
        None,
    )
    if named is not None and named.get("sha256") != digest:
        return {"item": named, "conflict_kind": "name"}
    return None


def _same_folder(item: dict, folder_id: str) -> bool:
    folders = item.get("folder_ids") if isinstance(item.get("folder_ids"), list) else []
    if folder_id:
        return folder_id in folders
    return not folders


def _stage_conflict(plane, account_id, library_id, stored, conflict, filename, kind, captured, source, folder_id, digest: str = "", size: int = 0, incoming=None) -> dict:
    import secrets

    lang = _lang(plane, account_id)
    kind_name = conflict["conflict_kind"]
    item = conflict["item"]
    conflict_id = "conf-" + secrets.token_hex(8)
    folder = _library_root(plane, account_id, library_id) / "staging"
    secure_mkdir(folder)
    if stored is not None:
        target = folder / f"{conflict_id}.bin"
        target.write_bytes(stored)
        target.chmod(0o600)
        digest = hashlib.sha256(stored).hexdigest()
        size = len(stored)
    elif incoming is not None:
        incoming.replace(folder / f"{conflict_id}.bin")
        (folder / f"{conflict_id}.bin").chmod(0o600)
    meta = {
        "id": conflict_id,
        "item_id": item["id"],
        "conflict_kind": kind_name,
        "filename": filename,
        "ext": kind["ext"],
        "mime": kind["mime"],
        "group": kind["group"],
        "digest": digest,
        "size": size,
        "captured": captured,
        "source": source,
        "folder_id": folder_id,
    }
    write_json(folder / f"{conflict_id}.json", meta)
    return {
        "result": "conflict",
        "id": item["id"],
        "conflict_id": conflict_id,
        "conflict_kind": kind_name,
        "reason": say(lang, "conflict_identical" if kind_name == "identical" else "conflict_name"),
        "default": "keep" if kind_name == "identical" else "",
        "version_count": len(item.get("versions") or []),
    }


def resolve_conflict(plane, account_id: str, library_id: str, conflict_id: str, decision: str, peer_ip: str) -> dict:
    import re as _re

    lang = _lang(plane, account_id)
    if not _re.fullmatch(r"conf-[0-9a-f]{16}", conflict_id) or decision not in {"keep", "replace"}:
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    folder = _library_root(plane, account_id, library_id) / "staging"
    meta_path = folder / f"{conflict_id}.json"
    blob_path = folder / f"{conflict_id}.bin"
    meta = read_json(meta_path, {})
    if not isinstance(meta, dict) or meta.get("id") != conflict_id:
        raise SsiosError("library_missing", say(lang, "library_missing"), 404)
    kind_name = str(meta.get("conflict_kind") or "")
    if decision == "keep":
        _audit(plane, account_id, peer_ip, "library.keep", str(meta.get("item_id") or ""), "kept", int(meta.get("size") or 0), kind_name)
        _drop_stage(meta_path, blob_path)
        return {"status": "kept", "code": kind_name, "reason": say(lang, "conflict_kept"), "id": meta.get("item_id") or "", "path": meta.get("filename") or ""}
    digest = str(meta.get("digest") or "")
    ext = str(meta.get("ext") or "")
    if blob_path.is_file():
        _publish_incoming(plane, account_id, library_id, blob_path, digest, ext)
    elif not (_library_root(plane, account_id, library_id) / "objects" / f"{digest}.{ext}").is_file():
        raise SsiosError("library_missing", say(lang, "library_missing"), 404)
    saved = _record_item(
        plane,
        account_id,
        library_id,
        filename=str(meta.get("filename") or ""),
        kind={"ext": meta.get("ext"), "mime": meta.get("mime"), "group": meta.get("group")},
        digest=str(meta.get("digest") or ""),
        size=int(meta.get("size") or 0),
        captured=str(meta.get("captured") or ""),
        source=str(meta.get("source") or "drag"),
        peer_ip=peer_ip,
        folder_id=str(meta.get("folder_id") or ""),
        existing_id=str(meta.get("item_id") or ""),
    )
    _audit(plane, account_id, peer_ip, "library.replace", saved["id"], "version", int(meta.get("size") or 0), kind_name)
    _drop_stage(meta_path, blob_path)
    return {"status": "version", "code": kind_name, "reason": say(lang, "drop_version"), "id": saved["id"], "path": meta.get("filename") or ""}


def _drop_stage(meta_path, blob_path) -> None:
    for path in (meta_path, blob_path):
        if path.is_file() and not path.is_symlink():
            path.unlink()


def _put_blob(plane, account_id: str, library_id: str, digest: str, ext: str, stored: bytes) -> None:
    folder = _library_root(plane, account_id, library_id) / "objects"
    secure_mkdir(folder)
    path = folder / f"{digest}.{ext}"
    if path.exists():
        return
    path.write_bytes(stored)
    path.chmod(0o600)


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A tiny thumbnail tile. Used when the original is not a browser image."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _metadata(plane, account_id: str, library_id: str, *, create: bool, name: str) -> dict:
    if not _ID.fullmatch(library_id):
        raise SsiosError("library_missing", say(_lang(plane, account_id), "library_missing"), 404)
    path = _library_root(plane, account_id, library_id) / "metadata.json"
    if path.is_file():
        data = read_json(path, {})
        if isinstance(data, dict):
            return data
    if not create:
        raise SsiosError("library_missing", say(_lang(plane, account_id), "library_missing"), 404)
    data = {
        "id": library_id,
        "name": name or "Personal",
        "kind": "personal",
        "folders": [],
        "tags": [],
        "tag_groups": [],
        "smart_folders": [],
    }
    secure_mkdir(path.parent)
    write_json(path, data)
    return data


def _write_metadata(plane, account_id: str, library_id: str, data: dict) -> None:
    write_json(_library_root(plane, account_id, library_id) / "metadata.json", data)


def _items(plane, account_id: str, library_id: str) -> list[dict]:
    folder = _library_root(plane, account_id, library_id) / "items"
    if not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("item-*.json")):
        if path.is_symlink():
            continue
        data = read_json(path, {})
        if isinstance(data, dict) and data.get("id"):
            rows.append(data)
    return rows


def _write_item(plane, account_id: str, library_id: str, item: dict) -> None:
    folder = _library_root(plane, account_id, library_id) / "items"
    secure_mkdir(folder)
    write_json(folder / f"{item['id']}.json", item)


def _item_path(plane, account_id: str, library_id: str, item_id: str):
    return _library_root(plane, account_id, library_id) / "items" / f"{item_id}.json"


def _library_root(plane, account_id: str, library_id: str):
    if not _ID.fullmatch(library_id):
        raise SsiosError("path_rejected", "library id rejected", 400)
    root = plane.accounts.home(account_id) / "library" / library_id
    secure_mkdir(root)
    return root


def _lang(plane, account_id: str) -> str:
    try:
        lang = plane.skills.account_prefs(account_id).get("language")
    except Exception:
        lang = "en"
    return lang if lang in {"en", "zh"} else "en"


def _audit(plane, account_id: str, peer_ip: str, action: str, target: str, result: str, size: int, kind_name: str = "") -> None:
    extra = {"service": "library", "byte_count": str(int(size))}
    if kind_name in {"identical", "name"}:
        extra["reason_code"] = kind_name
    plane.security_audit.write(
        actor=account_id,
        ip=peer_ip or "127.0.0.1",
        action=action,
        target=str(target)[:80],
        result=result,
        extra=extra,
    )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

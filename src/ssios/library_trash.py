"""Deleting in Files: permanent delete of trashed files, empty trash, automatic emptying, and folders.

A file is deleted in two steps, as in Eagle: first to the trash, from where it can be restored, then for good.
Deleting for good removes the item row, its preview, and every stored file that no other item, version, or
waiting duplicate still uses. Deleting a folder never deletes files: they leave the folder and stay in the
library. Emptying the trash on a timer is off until the owner picks a number of days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ssios.errors import SsiosError
from ssios.locale_messages import say

RETENTION_CHOICES = (0, 7, 30, 60, 90)


def purge_many(plane, account_id: str, library_id: str, ids, peer_ip: str) -> int:
    """Delete trashed items for good. Items not in the trash are left alone. Returns how many went."""
    from ssios.library import _LOCK, _audit, _item_path, _items, _library_root, _require, get_item
    from ssios.library_analyze import revoke_recall
    from ssios.library_share import reader_ids

    _require(plane, account_id, library_id, "owner")
    root = _library_root(plane, account_id, library_id)
    readers = reader_ids(plane, account_id, library_id)
    gone: list[dict] = []
    with _LOCK:
        for item_id in ids:
            try:
                item = get_item(plane, account_id, library_id, str(item_id))
            except SsiosError:
                continue
            if not item.get("trashed"):
                continue
            path = _item_path(plane, account_id, library_id, item["id"])
            if path.is_file() and not path.is_symlink():
                path.unlink()
            preview = root / "thumbs" / f"{item['id']}.png"
            if preview.is_file() and not preview.is_symlink():
                preview.unlink()
            gone.append(item)
        if gone:
            _collect(root, _items(plane, account_id, library_id), gone)
    for item in gone:
        for reader_id in readers:
            revoke_recall(plane, reader_id, str(item["id"]), peer_ip)
        _audit(plane, account_id, peer_ip, "library.purge", str(item["id"]), "ok", int(item.get("size") or 0))
    return len(gone)


def _collect(root, remaining: list[dict], gone: list[dict]) -> None:
    """Remove the stored files of ``gone`` that nothing left still points to."""
    import json

    cited: set[str] = set()
    for item in remaining:
        cited.add(str(item.get("sha256") or ""))
        for version in item.get("versions") or []:
            if isinstance(version, dict):
                cited.add(str(version.get("sha256") or ""))
    staging = root / "staging"
    if staging.is_dir():
        # A duplicate waiting for keep-or-replace may point at a stored file.
        for meta in staging.glob("*.json"):
            try:
                cited.add(str(json.loads(meta.read_text(encoding="utf-8")).get("digest") or ""))
            except (OSError, ValueError):
                continue
    candidates: set[str] = set()
    for item in gone:
        candidates.add(str(item.get("sha256") or ""))
        for version in item.get("versions") or []:
            if isinstance(version, dict):
                candidates.add(str(version.get("sha256") or ""))
    objects = root / "objects"
    for digest in candidates - cited:
        if len(digest) != 64 or not all(ch in "0123456789abcdef" for ch in digest):
            continue
        # The extension of an older version can differ, so every file of that hash goes.
        for blob in objects.glob(f"{digest}.*"):
            if blob.is_file() and not blob.is_symlink():
                blob.unlink()


def empty_trash(plane, account_id: str, library_id: str, confirm: str, peer_ip: str) -> int:
    from ssios.library import _items, _lang, _require

    _require(plane, account_id, library_id, "owner")
    if str(confirm or "").strip().casefold() not in {"delete", "删除"}:
        raise SsiosError("library_rejected", say(_lang(plane, account_id), "lib_empty_confirm_needed"), 400)
    ids = [item["id"] for item in _items(plane, account_id, library_id) if item.get("trashed")]
    return purge_many(plane, account_id, library_id, ids, peer_ip)


def set_retention(plane, account_id: str, library_id: str, days, peer_ip: str) -> int:
    from ssios.library import _audit, _lang, _require, metadata, replace_metadata

    _require(plane, account_id, library_id, "owner")
    try:
        value = int(days)
    except (TypeError, ValueError):
        value = -1
    if value not in RETENTION_CHOICES:
        raise SsiosError("library_rejected", say(_lang(plane, account_id), "library_rejected"), 400)
    data = metadata(plane, account_id, library_id)
    data["trash_days"] = value
    replace_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.trash_days", library_id, str(value), 0)
    return value


def expired(items: list[dict], days: int, now: datetime) -> list[str]:
    if days <= 0:
        return []
    cutoff = now - timedelta(days=days)
    out = []
    for item in items:
        if not item.get("trashed"):
            continue
        try:
            when = datetime.fromisoformat(str(item.get("trashed_at") or ""))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= cutoff:
            out.append(str(item["id"]))
    return out


def auto_empty(plane, account: dict, now: datetime | None = None) -> int:
    """The background pass: delete for good what has been in the trash longer than the owner chose."""
    from ssios.library import _items, list_libraries, metadata

    account_id = str(account.get("id") or "")
    now = now or datetime.now(timezone.utc)
    total = 0
    for row in list_libraries(plane, account_id):
        if row.get("offline") or (row.get("kind") == "shared" and row.get("role") != "owner"):
            continue
        library_id = str(row["id"])
        days = int(metadata(plane, account_id, library_id).get("trash_days") or 0)
        ids = expired(_items(plane, account_id, library_id), days, now)
        if ids:
            total += purge_many(plane, account_id, library_id, ids, "127.0.0.1")
    return total


def _descendants(folders: list[dict], folder_id: str) -> set[str]:
    found = {folder_id}
    changed = True
    while changed:
        changed = False
        for row in folders:
            if str(row.get("parent_id") or "") in found and str(row.get("id") or "") not in found:
                found.add(str(row["id"]))
                changed = True
    return found


def rename_folder(plane, account_id: str, library_id: str, folder_id: str, name: str, peer_ip: str) -> dict:
    from ssios.library import _LOCK, _audit, _lang, _metadata, _require, _write_metadata

    _require(plane, account_id, library_id, "organize")
    lang = _lang(plane, account_id)
    title = " ".join(str(name or "").split())[:40]
    if not title or "/" in title or title.startswith("."):
        raise SsiosError("library_folder_name", say(lang, "lib_folder_name"), 400)
    with _LOCK:
        data = _metadata(plane, account_id, library_id, create=False, name="")
        folders = [row for row in data.get("folders") or [] if isinstance(row, dict)]
        row = next((one for one in folders if one.get("id") == folder_id), None)
        if row is None:
            raise SsiosError("library_missing", say(lang, "library_missing"), 404)
        parent = str(row.get("parent_id") or "")
        if any(one is not row and str(one.get("parent_id") or "") == parent and str(one.get("name") or "").casefold() == title.casefold() for one in folders):
            raise SsiosError("library_folder_name", say(lang, "lib_folder_exists"), 400)
        row["name"] = title
        _write_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.folder_rename", folder_id, "ok", 0)
    return row


def delete_folder(plane, account_id: str, library_id: str, folder_id: str, peer_ip: str) -> dict:
    """Remove a folder and its subfolders. Their files stay in the library, in no folder or in their other ones."""
    from ssios.library import _LOCK, _audit, _items, _lang, _metadata, _require, _write_item, _write_metadata

    _require(plane, account_id, library_id, "organize")
    lang = _lang(plane, account_id)
    with _LOCK:
        data = _metadata(plane, account_id, library_id, create=False, name="")
        folders = [row for row in data.get("folders") or [] if isinstance(row, dict)]
        if not any(row.get("id") == folder_id for row in folders):
            raise SsiosError("library_missing", say(lang, "library_missing"), 404)
        removed = _descendants(folders, folder_id)
        data["folders"] = [row for row in folders if str(row.get("id") or "") not in removed]
        if isinstance(data.get("eagle_folders"), dict):
            data["eagle_folders"] = {key: value for key, value in data["eagle_folders"].items() if value not in removed}
        _write_metadata(plane, account_id, library_id, data)
        moved = 0
        for item in _items(plane, account_id, library_id):
            current = [str(one) for one in item.get("folder_ids") or []]
            kept = [one for one in current if one not in removed]
            if len(kept) != len(current):
                item["folder_ids"] = kept
                _write_item(plane, account_id, library_id, item)
                moved += 1
    _audit(plane, account_id, peer_ip, "library.folder_delete", folder_id, "ok", moved)
    return {"folders": len(removed), "items": moved}


def remove_from_folder(plane, account_id: str, library_id: str, item_id: str, folder_id: str, peer_ip: str) -> dict:
    from ssios.library import _LOCK, _audit, _require, _write_item, get_item

    _require(plane, account_id, library_id, "organize")
    with _LOCK:
        item = get_item(plane, account_id, library_id, item_id)
        item["folder_ids"] = [str(one) for one in item.get("folder_ids") or [] if str(one) != folder_id]
        _write_item(plane, account_id, library_id, item)
    _audit(plane, account_id, peer_ip, "library.unfolder", item_id, "ok", 0)
    return item

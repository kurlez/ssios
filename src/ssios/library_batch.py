"""Batch edits for the visible Files selection.

Each item is applied on its own. A failure on one id does not undo the others.
The response lists every id. One audit line is written per item that changes.
Download is one zip of the selected originals. The zip entry name is the item
id plus the display name, and the stored bytes are not rewritten.
"""

from __future__ import annotations

import io
import re
import zipfile

from ssios.errors import SsiosError
from ssios.library import _ITEM, _audit, _now, _require, _role_of, _write_item, get_item, item_bytes, purge_item
from ssios.locale_messages import say

_OPS = {
    "tag": "add",
    "untag": "add",
    "folder": "organize",
    "unfolder": "organize",
    "trash": "add",
    "restore": "add",
    "purge": "owner",
    "analyze": "add",
    "download": "view",
}


def parse_ids(raw) -> list[str]:
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, list):
        parts = raw
    else:
        parts = []
    found = []
    for part in parts:
        item_id = str(part or "").strip()
        if item_id and item_id not in found:
            found.append(item_id[:80])
        if len(found) == 200:
            break
    return found


def batch_apply(plane, account_id: str, library_id: str, raw_ids, op: str, peer_ip: str, *, tag: str = "", folder_id: str = "", confirm: str = "", model=None) -> dict:
    from ssios.library import _lang

    lang = _lang(plane, account_id)
    if op not in _OPS:
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    _require(plane, account_id, library_id, _OPS[op])
    role = _role_of(plane, account_id, library_id) or ""
    results = []
    for item_id in parse_ids(raw_ids):
        try:
            _one(plane, account_id, library_id, item_id, op, peer_ip, lang, role, tag=tag, folder_id=folder_id, confirm=confirm, model=model)
            results.append({"id": item_id, "ok": True})
        except SsiosError as exc:
            results.append({"id": item_id, "ok": False, "code": exc.code, "error": exc.detail})
    return {"results": results}


def zip_items(plane, account_id: str, library_id: str, raw_ids, peer_ip: str) -> bytes:
    """One zip. Missing ids are omitted and recorded as failed audit is not written for them."""
    _require(plane, account_id, library_id, "view")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item_id in parse_ids(raw_ids):
            if not _ITEM.fullmatch(item_id):
                continue
            try:
                item = get_item(plane, account_id, library_id, item_id)
                data, _mime = item_bytes(plane, account_id, library_id, item_id)
            except SsiosError:
                continue
            archive.writestr(_zip_name(item), data)
            _audit(plane, account_id, peer_ip, "library.batch_download", item_id, "ok", int(item.get("size") or 0))
    return buffer.getvalue()


def _one(plane, account_id, library_id, item_id, op, peer_ip, lang, role, *, tag, folder_id, confirm, model) -> None:
    if not _ITEM.fullmatch(item_id):
        raise SsiosError("library_missing", say(lang, "library_missing"), 404)
    item = get_item(plane, account_id, library_id, item_id)
    if op in {"trash", "restore"} and role != "owner" and str(item.get("added_by") or "") != account_id:
        raise SsiosError("library_denied", say(lang, "library_denied"), 403)
    if op == "tag":
        _tag(item, tag, lang, add=True)
    elif op == "untag":
        _tag(item, tag, lang, add=False)
    elif op == "folder":
        _folder(item, folder_id, lang, add=True)
    elif op == "unfolder":
        _folder(item, folder_id, lang, add=False)
    elif op == "trash":
        item["trashed"] = True
        item["trashed_at"] = _now()
    elif op == "restore":
        item["trashed"] = False
        item["trashed_at"] = ""
    elif op == "purge":
        purge_item(plane, account_id, library_id, item_id, confirm, peer_ip)
        _audit(plane, account_id, peer_ip, "library.batch_purge", item_id, "ok", int(item.get("size") or 0))
        return
    elif op == "analyze":
        from ssios.library_analyze import analyze_item

        analyze_item(plane, account_id, library_id, item_id, peer_ip, model=model)
        _audit(plane, account_id, peer_ip, "library.batch_analyze", item_id, "ok", int(item.get("size") or 0))
        return
    item["modified_at"] = _now()
    _write_item(plane, account_id, library_id, item)
    _audit(plane, account_id, peer_ip, "library.batch_" + op, item_id, "ok", int(item.get("size") or 0))


def _tag(item: dict, tag: str, lang: str, *, add: bool) -> None:
    title = " ".join(str(tag or "").split())[:40]
    if not title or "/" in title:
        raise SsiosError("library_rename", say(lang, "lib_batch_value"), 400)
    tags = [str(one) for one in item.get("tags") or []]
    if add and title not in tags:
        tags.append(title)
    if not add:
        tags = [one for one in tags if one != title]
    item["tags"] = tags[:40]


def _folder(item: dict, folder_id: str, lang: str, *, add: bool) -> None:
    if not re.fullmatch(r"fld-[0-9a-f]{8}", str(folder_id or "")):
        raise SsiosError("library_folder_name", say(lang, "lib_folder_name"), 400)
    folders = [str(one) for one in item.get("folder_ids") or []]
    if add and folder_id not in folders:
        folders.append(folder_id)
    if not add:
        folders = [one for one in folders if one != folder_id]
    item["folder_ids"] = folders


def _zip_name(item: dict) -> str:
    base = str(item.get("name") or "file")
    base = base.replace("\\", "/").split("/")[-1]
    base = re.sub(r"[\x00-\x1f]", "", base).strip(" .")[:80] or "file"
    return f"{item['id']}-{base}"

"""Household sharing. A private library stays on the account. A shared one has an owner-set ACL."""

from __future__ import annotations

import shutil

from ssios.errors import SsiosError
from ssios.library import _ID, _LOCK, _audit, _lang, _require, _shared_acl_path, ensure_personal
from ssios.locale_messages import say
from ssios.util import read_json, secure_mkdir, write_json

_ROLES = {"view", "add", "organize"}


def reader_ids(plane, account_id: str, library_id: str) -> list[str]:
    path = _shared_acl_path(plane, library_id)
    if not path.is_file():
        return [account_id]
    acl = read_json(path, {})
    found = []
    owner_id = str(acl.get("owner_id") or "")
    if owner_id:
        found.append(owner_id)
    for member in acl.get("members") or []:
        if isinstance(member, dict) and member.get("account_id") and member["account_id"] not in found:
            found.append(str(member["account_id"]))
    return found or [account_id]


def share_library(plane, owner_id: str, library_id: str, username: str, role: str, peer_ip: str) -> dict:
    lang = _lang(plane, owner_id)
    if not _ID.fullmatch(library_id) or role not in _ROLES:
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    if ensure_personal(plane, owner_id)["id"] == library_id and not _shared_acl_path(plane, library_id).is_file():
        raise SsiosError("library_denied", say(lang, "library_private"), 403)
    _require(plane, owner_id, library_id, "owner")
    member = _active(plane, username)
    if member["id"] == owner_id:
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    _publish(plane, owner_id, library_id)
    path = _shared_acl_path(plane, library_id)
    with _LOCK:
        acl = read_json(path, {})
        members = [item for item in acl.get("members") or [] if isinstance(item, dict) and item.get("account_id") != member["id"]]
        members.append({"account_id": member["id"], "role": role})
        acl["members"] = members
        write_json(path, acl)
        _remember(plane, member["id"], library_id, str(acl.get("name") or "Files"), owner_id)
    _audit(plane, owner_id, peer_ip, "library.share", member["id"], "ok", 0, role)
    _copy_notes(plane, owner_id, member["id"], library_id, peer_ip)
    return acl


def unshare_library(plane, owner_id: str, library_id: str, username: str, peer_ip: str) -> None:
    lang = _lang(plane, owner_id)
    _require(plane, owner_id, library_id, "owner")
    member = _active(plane, username)
    path = _shared_acl_path(plane, library_id)
    if not path.is_file():
        raise SsiosError("library_missing", say(lang, "library_missing"), 404)
    with _LOCK:
        acl = read_json(path, {})
        acl["members"] = [item for item in acl.get("members") or [] if not (isinstance(item, dict) and item.get("account_id") == member["id"])]
        write_json(path, acl)
        _forget_pointer(plane, member["id"], library_id)
    from ssios.library import _items
    from ssios.library_analyze import revoke_recall

    for item in _items(plane, owner_id, library_id):
        revoke_recall(plane, member["id"], str(item.get("id") or ""), peer_ip)
    _audit(plane, owner_id, peer_ip, "library.share", member["id"], "revoked", 0)


def _publish(plane, owner_id: str, library_id: str) -> None:
    path = _shared_acl_path(plane, library_id)
    if path.is_file():
        return
    source = plane.accounts.home(owner_id) / "library" / library_id
    if not (source / "metadata.json").is_file():
        raise SsiosError("library_missing", say(_lang(plane, owner_id), "library_missing"), 404)
    meta = read_json(source / "metadata.json", {})
    dest = path.parent
    secure_mkdir(dest.parent)
    with _LOCK:
        shutil.move(str(source), str(dest))
        acl = {"id": library_id, "name": str(meta.get("name") or "Files"), "owner_id": owner_id, "members": []}
        write_json(path, acl)
        index_path = plane.accounts.home(owner_id) / "library" / "libraries.json"
        index = read_json(index_path, {"libraries": []})
        for row in index.get("libraries") or []:
            if isinstance(row, dict) and row.get("id") == library_id:
                row["kind"] = "shared"
        write_json(index_path, index)


def _remember(plane, account_id: str, library_id: str, name: str, owner_id: str) -> None:
    ensure_personal(plane, account_id)
    index_path = plane.accounts.home(account_id) / "library" / "libraries.json"
    index = read_json(index_path, {"libraries": []})
    libraries = index.setdefault("libraries", [])
    for row in libraries:
        if isinstance(row, dict) and row.get("id") == library_id:
            row["kind"] = "shared"
            row["name"] = name
            row["owner_id"] = owner_id
            write_json(index_path, index)
            return
    libraries.append({"id": library_id, "name": name, "kind": "shared", "owner_id": owner_id})
    write_json(index_path, index)


def _forget_pointer(plane, account_id: str, library_id: str) -> None:
    index_path = plane.accounts.home(account_id) / "library" / "libraries.json"
    index = read_json(index_path, {"libraries": []})
    index["libraries"] = [row for row in index.get("libraries") or [] if not (isinstance(row, dict) and row.get("id") == library_id)]
    write_json(index_path, index)


def _copy_notes(plane, owner_id: str, member_id: str, library_id: str, peer_ip: str) -> None:
    from ssios.library import _items
    from ssios.library_analyze import _write_topic

    for item in _items(plane, owner_id, library_id):
        if not (item.get("description") or item.get("ocr")):
            continue
        _write_topic(plane, member_id, library_id, item, peer_ip)


def _active(plane, username: str) -> dict:
    for item in plane.accounts.public_list():
        if item.get("username") == username and item.get("status") == "active":
            return item
    raise SsiosError("library_missing", say("en", "library_missing"), 404)

"""The tag manager: rename, merge, and delete a tag across a library, and tag groups with a colour.

A rename onto a name that already exists is a merge: the items that had either tag end with the one name.
Deleting a tag takes it off every item; the items stay. Groups hold tag names and a colour, as Eagle's tag groups
do, and groups imported from Eagle keep their colours. Nothing here deletes a file.
"""

from __future__ import annotations

import hashlib
import re

from ssios.errors import SsiosError
from ssios.locale_messages import say

# Eagle stores a group colour by name. Files keeps the name and draws it with these.
NAMED = {
    "red": "#e53935",
    "orange": "#fb8c00",
    "yellow": "#fdd835",
    "green": "#43a047",
    "aqua": "#00acc1",
    "blue": "#1e88e5",
    "purple": "#8e24aa",
    "pink": "#ec407a",
}
_HEX = re.compile(r"#[0-9a-fA-F]{6}")
_GROUP = re.compile(r"[A-Za-z0-9_-]{1,40}")


def colour_of(value: str) -> str:
    """A colour the page can draw: a named Eagle colour or #rrggbb. Anything else is no colour."""
    text = str(value or "").strip()
    if text.casefold() in NAMED:
        return NAMED[text.casefold()]
    return text.lower() if _HEX.fullmatch(text) else ""


def _clean(name: str, lang: str) -> str:
    title = " ".join(str(name or "").split()).lstrip("#")[:40]
    if not title or "/" in title:
        raise SsiosError("library_tag_name", say(lang, "lib_tag_name"), 400)
    return title


def rename_tag(plane, account_id: str, library_id: str, old: str, new: str, peer_ip: str) -> dict:
    """Rename ``old`` to ``new`` on every item. When ``new`` already exists, the two become one (a merge)."""
    from ssios.library import _LOCK, _audit, _items, _lang, _metadata, _require, _write_item, _write_metadata

    _require(plane, account_id, library_id, "organize")
    lang = _lang(plane, account_id)
    source = _clean(old, lang)
    target = _clean(new, lang)
    changed = 0
    with _LOCK:
        data = _metadata(plane, account_id, library_id, create=False, name="")
        listed = [str(tag) for tag in data.get("tags") or []]
        existing = {tag.casefold(): tag for tag in listed}
        for item in _items(plane, account_id, library_id):
            for tag in item.get("tags") or []:
                existing.setdefault(str(tag).casefold(), str(tag))
        if source.casefold() not in existing:
            raise SsiosError("library_missing", say(lang, "library_missing"), 404)
        # Merging into a tag that exists keeps that tag's spelling, unless only the case changes.
        if target.casefold() != source.casefold() and target.casefold() in existing:
            target = existing[target.casefold()]
        merged = target.casefold() in existing and target.casefold() != source.casefold()
        for item in _items(plane, account_id, library_id):
            tags = [str(tag) for tag in item.get("tags") or []]
            if not any(tag.casefold() == source.casefold() for tag in tags):
                continue
            out: list[str] = []
            for tag in tags:
                value = target if tag.casefold() in {source.casefold(), target.casefold()} else tag
                if value.casefold() not in {one.casefold() for one in out}:
                    out.append(value)
            item["tags"] = out
            _write_item(plane, account_id, library_id, item)
            changed += 1
        kept: list[str] = []
        for tag in listed:
            value = target if tag.casefold() in {source.casefold(), target.casefold()} else tag
            if value.casefold() not in {one.casefold() for one in kept}:
                kept.append(value)
        if target.casefold() not in {one.casefold() for one in kept}:
            kept.append(target)
        data["tags"] = kept[:2000]
        for group in data.get("tag_groups") or []:
            if isinstance(group, dict):
                names: list[str] = []
                for tag in group.get("tags") or []:
                    value = target if str(tag).casefold() in {source.casefold(), target.casefold()} else str(tag)
                    if value.casefold() not in {one.casefold() for one in names}:
                        names.append(value)
                group["tags"] = names
        _write_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.tag_merge" if merged else "library.tag_rename", library_id, "ok", changed)
    return {"name": target, "items": changed, "merged": merged}


def delete_tag(plane, account_id: str, library_id: str, name: str, peer_ip: str) -> dict:
    """Take a tag off every item and out of the tag list and groups. The items stay."""
    from ssios.library import _LOCK, _audit, _items, _lang, _metadata, _require, _write_item, _write_metadata

    _require(plane, account_id, library_id, "organize")
    lang = _lang(plane, account_id)
    gone = _clean(name, lang).casefold()
    changed = 0
    with _LOCK:
        for item in _items(plane, account_id, library_id):
            tags = [str(tag) for tag in item.get("tags") or []]
            left = [tag for tag in tags if tag.casefold() != gone]
            if len(left) != len(tags):
                item["tags"] = left
                _write_item(plane, account_id, library_id, item)
                changed += 1
        data = _metadata(plane, account_id, library_id, create=False, name="")
        data["tags"] = [str(tag) for tag in data.get("tags") or [] if str(tag).casefold() != gone]
        for group in data.get("tag_groups") or []:
            if isinstance(group, dict):
                group["tags"] = [str(tag) for tag in group.get("tags") or [] if str(tag).casefold() != gone]
        _write_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.tag_delete", library_id, "ok", changed)
    return {"items": changed}


def remove_item_tag(plane, account_id: str, library_id: str, item_id: str, name: str, peer_ip: str) -> dict:
    """Take one tag off one item, from the inspector."""
    from ssios.library import _LOCK, _audit, _require, _write_item, get_item

    _require(plane, account_id, library_id, "add")
    gone = " ".join(str(name or "").split()).casefold()
    with _LOCK:
        item = get_item(plane, account_id, library_id, item_id)
        item["tags"] = [str(tag) for tag in item.get("tags") or [] if str(tag).casefold() != gone]
        _write_item(plane, account_id, library_id, item)
    _audit(plane, account_id, peer_ip, "library.untag", item_id, "ok", 0)
    return item


def add_group(plane, account_id: str, library_id: str, name: str, colour: str, peer_ip: str) -> dict:
    from ssios.library import _LOCK, _audit, _lang, _metadata, _now, _require, _write_metadata

    _require(plane, account_id, library_id, "organize")
    lang = _lang(plane, account_id)
    title = _clean(name, lang)
    with _LOCK:
        data = _metadata(plane, account_id, library_id, create=False, name="")
        groups = [row for row in data.get("tag_groups") or [] if isinstance(row, dict)]
        if any(str(row.get("name") or "").casefold() == title.casefold() for row in groups):
            raise SsiosError("library_tag_name", say(lang, "lib_group_exists"), 400)
        group_id = "tgr-" + hashlib.sha256(f"{library_id}:{title}:{_now()}".encode()).hexdigest()[:8]
        row = {"id": group_id, "name": title, "tags": [], "color": colour if colour_of(colour) else ""}
        groups.append(row)
        data["tag_groups"] = groups[:200]
        _write_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.tag_group", group_id, "ok", 0)
    return row


def update_group(plane, account_id: str, library_id: str, group_id: str, peer_ip: str, *, name: str | None = None, colour: str | None = None, delete: bool = False) -> None:
    from ssios.library import _LOCK, _audit, _lang, _metadata, _require, _write_metadata

    _require(plane, account_id, library_id, "organize")
    lang = _lang(plane, account_id)
    if not _GROUP.fullmatch(str(group_id or "")):
        raise SsiosError("library_missing", say(lang, "library_missing"), 404)
    with _LOCK:
        data = _metadata(plane, account_id, library_id, create=False, name="")
        groups = [row for row in data.get("tag_groups") or [] if isinstance(row, dict)]
        row = next((one for one in groups if str(one.get("id") or "") == group_id), None)
        if row is None:
            raise SsiosError("library_missing", say(lang, "library_missing"), 404)
        if delete:
            # The group goes; its tags stay on their items.
            groups.remove(row)
        else:
            if name is not None:
                row["name"] = _clean(name, lang)
            if colour is not None:
                row["color"] = colour if colour_of(colour) else ""
        data["tag_groups"] = groups
        _write_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.tag_group", group_id, "deleted" if delete else "ok", 0)


def set_tag_group(plane, account_id: str, library_id: str, tag: str, group_id: str, peer_ip: str) -> None:
    """Put a tag in one group, or in none with an empty ``group_id``. A tag is in at most one group."""
    from ssios.library import _LOCK, _audit, _lang, _metadata, _require, _write_metadata

    _require(plane, account_id, library_id, "organize")
    lang = _lang(plane, account_id)
    title = _clean(tag, lang)
    with _LOCK:
        data = _metadata(plane, account_id, library_id, create=False, name="")
        groups = [row for row in data.get("tag_groups") or [] if isinstance(row, dict)]
        if group_id and not any(str(row.get("id") or "") == group_id for row in groups):
            raise SsiosError("library_missing", say(lang, "library_missing"), 404)
        for row in groups:
            names = [str(one) for one in row.get("tags") or [] if str(one).casefold() != title.casefold()]
            if str(row.get("id") or "") == group_id:
                names.append(title)
            row["tags"] = names
        data["tag_groups"] = groups
        _write_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.tag_group", group_id or "none", "ok", 0)


def groups_of(meta: dict) -> tuple[list[dict], dict[str, dict]]:
    """The groups, and for each tag (by its folded name) the group it is in."""
    groups = [row for row in meta.get("tag_groups") or [] if isinstance(row, dict) and row.get("name")]
    owner: dict[str, dict] = {}
    for row in groups:
        for tag in row.get("tags") or []:
            owner.setdefault(str(tag).casefold(), row)
    return groups, owner

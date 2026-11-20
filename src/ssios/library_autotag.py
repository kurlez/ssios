"""Tags that Files adds by itself, and the rule that a model's tags are only suggestions.

Three kinds, as in Eagle:

* Folder auto-tags. A folder can carry tags; a file that is imported into it or moved into it gets them.
* Rule tags, chosen per library and off until the owner turns them on: the file's type, its orientation, and
  the names of the subfolders of a dropped folder.
* Model tags. Analysis, on demand or in the background, puts its tags in ``suggested_tags``. They are not tags
  until the owner accepts them, one by one or all at once. A dismissed suggestion is not suggested again.

A tag that differs from a tag the library already has only by case, spacing, or a leading ``#`` becomes that tag.
"""

from __future__ import annotations

from ssios.errors import SsiosError
from ssios.locale_messages import say

RULES = ("type", "orientation", "folder_names")
_TYPE_NAMES = {
    "image": ("image", "图片"),
    "video": ("video", "视频"),
    "audio": ("audio", "音频"),
    "document": ("document", "文档"),
    "design": ("design", "设计"),
    "font": ("font", "字体"),
}
_SHAPE_NAMES = {"landscape": ("landscape", "横向"), "portrait": ("portrait", "纵向"), "square": ("square", "方形")}


def tag_key(name: str) -> str:
    return " ".join(str(name or "").split()).lstrip("#").strip().casefold()


def known_tags(plane, account_id: str, library_id: str, meta: dict | None = None) -> dict[str, str]:
    """Every tag spelling in use, by its key. The library list first, then what items carry."""
    from ssios.library import _items, metadata

    data = meta if meta is not None else metadata(plane, account_id, library_id)
    known: dict[str, str] = {}
    for tag in data.get("tags") or []:
        known.setdefault(tag_key(tag), str(tag))
    for item in _items(plane, account_id, library_id):
        for tag in item.get("tags") or []:
            known.setdefault(tag_key(tag), str(tag))
    known.pop("", None)
    return known


def canonical(tags, known: dict[str, str]) -> list[str]:
    """Clean tags and give each the spelling the library already uses."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        title = " ".join(str(tag or "").split()).lstrip("#").strip()[:40]
        key = title.casefold()
        if not title or "/" in title or key in seen:
            continue
        seen.add(key)
        out.append(known.get(key, title))
    return out


def rules_of(meta: dict) -> dict:
    raw = meta.get("rule_tags") if isinstance(meta.get("rule_tags"), dict) else {}
    return {name: bool(raw.get(name)) for name in RULES}


def folder_tags(meta: dict, folder_ids) -> list[str]:
    wanted = {str(one) for one in folder_ids or []}
    out: list[str] = []
    for row in meta.get("folders") or []:
        if isinstance(row, dict) and str(row.get("id") or "") in wanted:
            out.extend(str(tag) for tag in row.get("auto_tags") or [])
    return out


def rule_tags(meta: dict, item: dict, lang: str, folder_names: list[str] | None = None) -> list[str]:
    rules = rules_of(meta)
    pick = 1 if lang == "zh" else 0
    out: list[str] = []
    if rules["type"] and str(item.get("group") or "") in _TYPE_NAMES:
        out.append(_TYPE_NAMES[str(item["group"])][pick])
    if rules["orientation"] and str(item.get("orientation") or "") in _SHAPE_NAMES:
        out.append(_SHAPE_NAMES[str(item["orientation"])][pick])
    if rules["folder_names"]:
        out.extend(str(name) for name in folder_names or [])
    return out


def on_import(plane, account_id: str, library_id: str, item_id: str, folder_ids, peer_ip: str, *, new: bool, folder_names: list[str] | None = None) -> list[str]:
    """Folder auto-tags, and rule tags for a new file. Returns the tags added."""
    from ssios.library import _lang, metadata

    meta = metadata(plane, account_id, library_id)
    from ssios.library import get_item

    item = get_item(plane, account_id, library_id, item_id)
    wanted = folder_tags(meta, folder_ids)
    if new:
        wanted += rule_tags(meta, item, _lang(plane, account_id), folder_names)
    return _add(plane, account_id, library_id, item, wanted, meta, peer_ip)


def on_move(plane, account_id: str, library_id: str, item_id: str, folder_id: str, peer_ip: str) -> list[str]:
    from ssios.library import get_item, metadata

    meta = metadata(plane, account_id, library_id)
    wanted = folder_tags(meta, [folder_id])
    if not wanted:
        return []
    return _add(plane, account_id, library_id, get_item(plane, account_id, library_id, item_id), wanted, meta, peer_ip)


def _add(plane, account_id: str, library_id: str, item: dict, wanted: list[str], meta: dict, peer_ip: str) -> list[str]:
    from ssios.library import add_item_tags

    if not wanted:
        return []
    have = {tag_key(tag) for tag in item.get("tags") or []}
    tags = [tag for tag in canonical(wanted, known_tags(plane, account_id, library_id, meta)) if tag_key(tag) not in have]
    if tags:
        add_item_tags(plane, account_id, library_id, str(item["id"]), tags, peer_ip)
    return tags


def set_folder_tags(plane, account_id: str, library_id: str, folder_id: str, raw, peer_ip: str, *, apply_now: bool = False) -> dict:
    """Give a folder its auto-tags. ``apply_now`` adds them to the files already in it."""
    from ssios.library import _LOCK, _audit, _items, _lang, _metadata, _require, _write_metadata, clean_tags

    _require(plane, account_id, library_id, "organize")
    lang = _lang(plane, account_id)
    with _LOCK:
        data = _metadata(plane, account_id, library_id, create=False, name="")
        row = next((one for one in data.get("folders") or [] if isinstance(one, dict) and one.get("id") == folder_id), None)
        if row is None:
            raise SsiosError("library_missing", say(lang, "library_missing"), 404)
        known = {tag_key(tag): str(tag) for tag in data.get("tags") or []}
        row["auto_tags"] = canonical(clean_tags(raw), known)[:20]
        _write_metadata(plane, account_id, library_id, data)
        tags = list(row["auto_tags"])
    added = 0
    if apply_now and tags:
        for item in _items(plane, account_id, library_id):
            if folder_id in (item.get("folder_ids") or []) and not item.get("trashed"):
                if on_move(plane, account_id, library_id, str(item["id"]), folder_id, peer_ip):
                    added += 1
    _audit(plane, account_id, peer_ip, "library.folder_tags", folder_id, "ok", added)
    return {"tags": tags, "items": added}


def set_rules(plane, account_id: str, library_id: str, chosen: dict, peer_ip: str) -> dict:
    from ssios.library import _audit, _require, metadata, replace_metadata

    _require(plane, account_id, library_id, "owner")
    data = metadata(plane, account_id, library_id)
    data["rule_tags"] = {name: bool(chosen.get(name)) for name in RULES}
    replace_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.rule_tags", library_id, "ok", 0)
    return data["rule_tags"]


def suggest(item: dict, tags: list[str], known: dict[str, str]) -> list[str]:
    """What analysis may suggest: not already a tag on the item, and not dismissed before."""
    have = {tag_key(tag) for tag in item.get("tags") or []}
    dismissed = {tag_key(tag) for tag in item.get("dismissed_tags") or []}
    current = [str(tag) for tag in item.get("suggested_tags") or []]
    for tag in canonical(tags, known):
        key = tag_key(tag)
        if key not in have and key not in dismissed and key not in {tag_key(one) for one in current}:
            current.append(tag)
    return current[:12]


def review(plane, account_id: str, library_id: str, item_id: str, peer_ip: str, *, accept=None, dismiss=None) -> dict:
    """Accept or dismiss suggested tags. ``"*"`` means all of them."""
    from ssios.library import _LOCK, _audit, _require, _write_item, get_item

    _require(plane, account_id, library_id, "add")
    known = known_tags(plane, account_id, library_id)
    with _LOCK:
        item = get_item(plane, account_id, library_id, item_id)
        offered = [str(tag) for tag in item.get("suggested_tags") or []]

        def chosen(value) -> list[str]:
            if value == "*":
                return list(offered)
            wanted = {tag_key(one) for one in (value if isinstance(value, list) else [value]) if one}
            return [tag for tag in offered if tag_key(tag) in wanted]

        taken = chosen(accept) if accept else []
        dropped = chosen(dismiss) if dismiss else []
        tags = [str(tag) for tag in item.get("tags") or []]
        have = {tag_key(tag) for tag in tags}
        for tag in canonical(taken, known):
            if tag_key(tag) not in have:
                tags.append(tag)
                have.add(tag_key(tag))
        done = {tag_key(tag) for tag in taken + dropped}
        item["tags"] = tags[:40]
        item["suggested_tags"] = [tag for tag in offered if tag_key(tag) not in done]
        if dropped:
            item["dismissed_tags"] = (list(item.get("dismissed_tags") or []) + dropped)[-100:]
        _write_item(plane, account_id, library_id, item)
    if taken:
        from ssios.library import _metadata, _write_metadata

        with _LOCK:
            data = _metadata(plane, account_id, library_id, create=False, name="")
            listed = [str(tag) for tag in data.get("tags") or []]
            keys = {tag_key(tag) for tag in listed}
            extra = [tag for tag in canonical(taken, known) if tag_key(tag) not in keys]
            if extra:
                data["tags"] = (listed + extra)[:2000]
                _write_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.suggestions", item_id, "ok", len(taken))
    return {"accepted": taken, "dismissed": dropped, "item": item}


def review_all(plane, account_id: str, library_id: str, peer_ip: str, *, accept: bool, ids=None) -> int:
    """Accept or dismiss every suggestion in the library, or on the given items."""
    from ssios.library import _items

    wanted = set(ids or [])
    count = 0
    for item in _items(plane, account_id, library_id):
        if item.get("trashed") or not item.get("suggested_tags") or (wanted and item["id"] not in wanted):
            continue
        result = review(plane, account_id, library_id, str(item["id"]), peer_ip, accept="*" if accept else None, dismiss=None if accept else "*")
        count += len(result["accepted"] or result["dismissed"])
    return count

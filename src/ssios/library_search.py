"""Keyword search over names, tags, notes, and text read from a file."""

from __future__ import annotations

from ssios.errors import SsiosError
from ssios.library import list_items
from ssios.library_query import _blob
from ssios.locale_messages import say


def search_items(plane, account_id: str, library_id: str, query: str, *, ext: str = "", tag: str = "", min_star: int | None = None) -> list[dict]:
    from ssios.library import _lang

    text = " ".join(str(query or "").split())
    if len(text) > 200:
        raise SsiosError("library_rejected", say(_lang(plane, account_id), "library_rejected"), 400)
    needle = text.casefold()
    found = []
    for item in list_items(plane, account_id, library_id):
        if item.get("trashed"):
            continue
        if ext and str(item.get("ext") or "").casefold() != ext.casefold().lstrip("."):
            continue
        tags = [str(one) for one in item.get("tags") or []]
        if tag and tag not in tags:
            continue
        if min_star is not None and int(item.get("star") or 0) < min_star:
            continue
        if needle and needle not in _blob(item):
            continue
        found.append(item)
    return found

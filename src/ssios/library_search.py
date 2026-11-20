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
    close = meaning_ids(plane, account_id, library_id, text)
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
        if needle and needle not in _blob(item) and str(item.get("id") or "") not in close:
            continue
        found.append(item)
    return by_meaning(found, close, needle)


def meaning_ids(plane, account_id: str, library_id: str, query: str) -> list[str]:
    """Ids of live items close in meaning to the query, best first. Empty when meaning search is off."""
    from ssios.library import _library_root
    from ssios.semantic import embedder_for, library_hits

    text = " ".join(str(query or "").split())
    if not text or embedder_for(plane) is None:
        return []
    live = [item for item in list_items(plane, account_id, library_id) if not item.get("trashed")]
    return library_hits(plane, _library_root(plane, account_id, library_id), library_id, live, text)


def by_meaning(items: list[dict], close: list[str], needle: str) -> list[dict]:
    """Keyword matches keep their order. Items found only by meaning follow, closest first."""
    if not close:
        return items
    order = {item_id: index for index, item_id in enumerate(close)}
    exact = [item for item in items if needle in _blob(item)]
    extra = [item for item in items if needle not in _blob(item)]
    extra.sort(key=lambda item: order.get(str(item.get("id") or ""), len(order)))
    return exact + extra

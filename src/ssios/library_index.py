"""The index fields of a Files item: size in pixels, orientation, duration, and main colours.

New files get them at import. Files imported before this version get them from ``ssios library-reindex`` or,
a few at a time, from the host's background timer. ``INDEX_VERSION`` goes up when a new field is added, so
older rows are filled again.
"""

from __future__ import annotations

import time
from pathlib import Path

from ssios.library_meta import file_info
from ssios.library_palette import palette_of

INDEX_VERSION = 1
FIELDS = ("width", "height", "orientation", "duration", "palette")
# Files above this size get their colours from the background pass, not while the upload waits.
_INLINE_BYTES = 40_000_000


def index_fields(root: Path, digest: str, ext: str, item_id: str = "", *, inline: bool = True) -> dict:
    """What the index records for one stored file. ``inline`` is an import: big files leave the colours for later."""
    path = root / "objects" / f"{digest}.{ext}"
    fields = dict(file_info(path, ext))
    try:
        size = path.stat().st_size
    except OSError:
        return fields
    if inline and size > _INLINE_BYTES:
        return fields
    preview = root / "thumbs" / f"{item_id}.png" if item_id else None
    colours = palette_of(path, ext, preview)
    if colours:
        fields["palette"] = colours
    fields["indexed"] = INDEX_VERSION
    return fields


def needs_index(item: dict) -> bool:
    return int(item.get("indexed") or 0) < INDEX_VERSION and not item.get("trashed")


def index_item(plane, account_id: str, library_id: str, item_id: str, *, force: bool = False) -> bool:
    """Fill the index fields of one item. True when the row was written."""
    from ssios.library import _LOCK, _library_root, _write_item, get_item

    item = get_item(plane, account_id, library_id, item_id)
    if not force and not needs_index(item):
        return False
    root = _library_root(plane, account_id, library_id)
    fields = index_fields(root, str(item.get("sha256") or ""), str(item.get("ext") or ""), item_id, inline=False)
    fields["indexed"] = INDEX_VERSION
    with _LOCK:
        # Read again under the lock so an edit made while the colours were worked out is kept.
        current = get_item(plane, account_id, library_id, item_id)
        if current.get("sha256") != item.get("sha256"):
            return False
        for key in FIELDS:
            current.pop(key, None)
        current.update(fields)
        _write_item(plane, account_id, library_id, current)
    return True


def reindex_library(plane, account_id: str, library_id: str, *, force: bool = False, budget: float | None = None) -> dict:
    """Index every item that needs it, or every item with ``force``. Stops early when ``budget`` seconds pass."""
    from ssios.library import _items

    started = time.monotonic()
    done = skipped = failed = 0
    left = 0
    for item in _items(plane, account_id, library_id):
        if item.get("trashed") or (not force and not needs_index(item)):
            skipped += 1
            continue
        if budget is not None and time.monotonic() - started > budget:
            left += 1
            continue
        try:
            if index_item(plane, account_id, library_id, str(item["id"]), force=force):
                done += 1
            else:
                skipped += 1
        except Exception:
            failed += 1
    return {"indexed": done, "skipped": skipped, "failed": failed, "left": left}


def index_next(plane, account: dict, budget: float = 2.0) -> int:
    """The background pass: index older items in this account's own libraries for up to ``budget`` seconds."""
    from ssios.library import list_libraries

    account_id = str(account.get("id") or "")
    total = 0
    started = time.monotonic()
    for row in list_libraries(plane, account_id):
        if row.get("offline") or (row.get("kind") == "shared" and row.get("role") != "owner"):
            continue
        left = budget - (time.monotonic() - started)
        if left <= 0:
            break
        total += reindex_library(plane, account_id, str(row["id"]), budget=left)["indexed"]
    return total

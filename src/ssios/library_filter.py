"""The filter bar on the Files page, as Eagle has one above the grid.

Every filter is a plain query parameter, so a filtered view is a link that can be bookmarked or saved as a smart
folder. Filters narrow the rows of one catalog read; nothing is read again per filter.

    type=image,video   ext=png,jpg      star=3 (three or more)
    tags=a,b  tagmode=all|any|none      untagged=1 is the sidebar's own view
    added=today|7d|30d|365d  or  from=2026-01-01  to=2026-02-01
    size_min / size_max in MB           shape=landscape|portrait|square
    min_w / min_h in pixels             color=rrggbb
    sort=added|modified|name|size|star|pixels   dir=asc|desc
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ssios.library_palette import hex_rgb, near

GROUPS = ("image", "video", "audio", "document", "design", "font")
SHAPES = ("landscape", "portrait", "square")
ADDED = ("today", "7d", "30d", "365d")
SORTS = ("added", "modified", "name", "size", "star", "pixels")
TAG_MODES = ("all", "any", "none")
KEYS = ("type", "ext", "star", "tags", "tagmode", "added", "from", "to", "size_min", "size_max", "shape", "min_w", "min_h", "color")
# Twelve colours to pick from, the way Eagle's colour filter offers a row of swatches.
COLOURS = (
    ("e53935", "red", "红"),
    ("fb8c00", "orange", "橙"),
    ("fdd835", "yellow", "黄"),
    ("43a047", "green", "绿"),
    ("00acc1", "cyan", "青"),
    ("1e88e5", "blue", "蓝"),
    ("8e24aa", "purple", "紫"),
    ("ec407a", "pink", "粉"),
    ("795548", "brown", "棕"),
    ("212121", "black", "黑"),
    ("9e9e9e", "grey", "灰"),
    ("fafafa", "white", "白"),
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_EXT = re.compile(r"[a-z0-9]{1,8}")


def _list(value, limit: int = 20) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[,，]", str(value or "")):
        text = " ".join(part.split())[:40]
        if text and text.casefold() not in {one.casefold() for one in out}:
            out.append(text)
    return out[:limit]


def _number(value, low: float, high: float) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number != number or number < low or number > high:
        return None
    return number


def parse(query: dict) -> dict:
    """The filters a query asks for, cleaned. Anything that does not parse is left out, never an error."""
    found: dict = {}
    groups = [one for one in _list(query.get("type")) if one in GROUPS]
    if groups:
        found["type"] = groups
    exts = [one.casefold().lstrip(".") for one in _list(query.get("ext"))]
    exts = [one for one in exts if _EXT.fullmatch(one)]
    if exts:
        found["ext"] = exts
    star = _number(query.get("star"), 1, 5)
    if star is not None:
        found["star"] = int(star)
    tags = _list(query.get("tags"))
    if tags:
        found["tags"] = tags
        mode = str(query.get("tagmode") or "all")
        found["tagmode"] = mode if mode in TAG_MODES else "all"
    added = str(query.get("added") or "")
    if added in ADDED:
        found["added"] = added
    for key in ("from", "to"):
        value = str(query.get(key) or "")
        if _DATE.fullmatch(value):
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                continue
            found[key] = value
    for key in ("size_min", "size_max"):
        value = _number(query.get(key), 0, 1_000_000)
        if value is not None:
            found[key] = value
    shape = str(query.get("shape") or "")
    if shape in SHAPES:
        found["shape"] = shape
    for key in ("min_w", "min_h"):
        value = _number(query.get(key), 1, 100_000)
        if value is not None:
            found[key] = int(value)
    colour = str(query.get("color") or "").strip().lstrip("#").casefold()
    if hex_rgb(colour) is not None and len(colour) == 6:
        found["color"] = colour
    return found


def sort_of(query: dict) -> tuple[str, bool]:
    """The sort key and whether it runs largest or newest first. The old names still work."""
    key = str(query.get("sort") or "added")
    old = {"newest": ("added", True)}
    if key in old:
        key, descending = old[key]
    else:
        if key not in SORTS:
            key = "added"
        descending = True if key in {"added", "modified", "size", "star", "pixels"} else False
    direction = str(query.get("dir") or "")
    if direction in {"asc", "desc"}:
        descending = direction == "desc"
    return key, descending


def _stamp(value) -> str:
    return str(value or "")


def _since(rule: str, now: datetime) -> str:
    if rule == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=int(rule[:-1]))
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def apply(items: list[dict], filters: dict, now: datetime | None = None) -> list[dict]:
    if not filters:
        return items
    now = now or datetime.now().astimezone()
    since = _since(filters["added"], now) if filters.get("added") else ""
    start = filters.get("from", "")
    end = ""
    if filters.get("to"):
        end = (datetime.strptime(filters["to"], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    wanted_tags = [one.casefold() for one in filters.get("tags") or []]
    mode = filters.get("tagmode", "all")
    low = filters.get("size_min")
    high = filters.get("size_max")
    out = []
    for item in items:
        if filters.get("type") and str(item.get("group") or "") not in filters["type"]:
            continue
        if filters.get("ext") and str(item.get("ext") or "").casefold() not in filters["ext"]:
            continue
        if filters.get("star") and int(item.get("star") or 0) < filters["star"]:
            continue
        if wanted_tags:
            have = {str(tag).casefold() for tag in item.get("tags") or []}
            if mode == "all" and not all(tag in have for tag in wanted_tags):
                continue
            if mode == "any" and not any(tag in have for tag in wanted_tags):
                continue
            if mode == "none" and any(tag in have for tag in wanted_tags):
                continue
        added = _stamp(item.get("imported_at"))
        if since and added < since:
            continue
        if start and added[:10] < start:
            continue
        if end and added[:10] >= end:
            continue
        size = int(item.get("size") or 0)
        if low is not None and size < low * 1_000_000:
            continue
        if high is not None and size > high * 1_000_000:
            continue
        if filters.get("shape") and str(item.get("orientation") or "") != filters["shape"]:
            continue
        if filters.get("min_w") and int(item.get("width") or 0) < filters["min_w"]:
            continue
        if filters.get("min_h") and int(item.get("height") or 0) < filters["min_h"]:
            continue
        if filters.get("color") and not near(item.get("palette") or [], filters["color"]):
            continue
        out.append(item)
    return out


def sort_items(items: list[dict], key: str, descending: bool) -> list[dict]:
    def value(item: dict):
        if key == "name":
            return str(item.get("name") or "").casefold()
        if key == "size":
            return int(item.get("size") or 0)
        if key == "star":
            return (int(item.get("star") or 0), str(item.get("imported_at") or ""))
        if key == "pixels":
            return int(item.get("width") or 0) * int(item.get("height") or 0)
        if key == "modified":
            return str(item.get("modified_at") or item.get("imported_at") or "")
        return str(item.get("imported_at") or "")

    return sorted(items, key=value, reverse=descending)


def as_query(filters: dict) -> dict:
    """The filters as query parameters again, for links and for a saved smart folder."""
    out: dict = {}
    for key in KEYS:
        if key not in filters:
            continue
        value = filters[key]
        if isinstance(value, list):
            out[key] = ",".join(value)
        elif isinstance(value, float):
            out[key] = ("%g" % value)
        else:
            out[key] = str(value)
    return out


def describe(filters: dict, lang: str) -> list[str]:
    """Short labels for the active filters, shown as chips above the grid."""
    zh = lang == "zh"
    labels = []
    if filters.get("type"):
        names = {"image": ("图片", "Images"), "video": ("视频", "Video"), "audio": ("音频", "Audio"), "document": ("文档", "Documents"), "design": ("设计文件", "Design files"), "font": ("字体", "Fonts")}
        labels.append(("类型：" if zh else "Type: ") + ", ".join(names[one][0 if zh else 1] for one in filters["type"]))
    if filters.get("ext"):
        labels.append(("扩展名 " if zh else "Extension ") + ", ".join(filters["ext"]))
    if filters.get("star"):
        labels.append(f"★ ≥ {filters['star']}")
    if filters.get("tags"):
        mode = {"all": ("全部", "all of"), "any": ("任一", "any of"), "none": ("排除", "none of")}[filters.get("tagmode", "all")]
        labels.append(("标签" + mode[0] + " " if zh else f"Tags, {mode[1]}: ") + ", ".join(filters["tags"]))
    if filters.get("added"):
        names = {"today": ("今天", "today"), "7d": ("7 天内", "last 7 days"), "30d": ("30 天内", "last 30 days"), "365d": ("一年内", "last year")}
        labels.append(("添加于 " if zh else "Added ") + names[filters["added"]][0 if zh else 1])
    if filters.get("from") or filters.get("to"):
        labels.append(f"{filters.get('from', '…')} – {filters.get('to', '…')}")
    if "size_min" in filters or "size_max" in filters:
        labels.append(f"{'%g' % filters.get('size_min', 0)}–{('%g' % filters['size_max']) if 'size_max' in filters else '∞'} MB")
    if filters.get("shape"):
        labels.append({"landscape": ("横向", "Landscape"), "portrait": ("纵向", "Portrait"), "square": ("方形", "Square")}[filters["shape"]][0 if zh else 1])
    if filters.get("min_w") or filters.get("min_h"):
        labels.append(f"≥ {filters.get('min_w', 0)} × {filters.get('min_h', 0)}")
    if filters.get("color"):
        labels.append(("颜色 #" if zh else "Colour #") + filters["color"])
    return labels

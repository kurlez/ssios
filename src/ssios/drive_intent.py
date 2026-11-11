"""Drive list built from the sentence. Root, a folder name, recent files, or a file name."""

from __future__ import annotations

import re

from ssios.errors import SsiosError

_FOLDER_ID = re.compile(r"folder:([A-Za-z0-9_-]{8,})")
_FOLDER_NAMED = (
    re.compile(r"名为\s*(.{1,40}?)\s*的文件夹"),
    re.compile(r"文件夹[「\"'](.{1,40})[」\"']"),
    re.compile(r"(?:in|inside)\s+(?:the\s+)?(.{1,40}?)\s+folder", re.IGNORECASE),
    re.compile(r"folder\s+(?:named\s+)?(.{1,40}?)(?:\s+in\b|\s+on\b|$)", re.IGNORECASE),
)
_SEARCH = (
    re.compile(r"(?:search|find)\s+(?:google\s+drive|my\s+drive|drive)\s+for\s+(.{1,60})", re.IGNORECASE),
    re.compile(r"(?:搜索|查找)\s*(.{1,40})"),
)
_RECENT = re.compile(r"最近|recent|latest", re.IGNORECASE)


def plan_drive(message: str) -> dict:
    text = " ".join(str(message or "").split())
    found = _FOLDER_ID.search(text)
    if found:
        return {"mode": "folder_id", "folder_id": found.group(1)}
    named = _folder_name(text)
    if named:
        return {"mode": "folder_name", "folder_name": named}
    keyword = _keyword(text)
    if keyword:
        return {"mode": "name", "keyword": keyword}
    if _RECENT.search(text):
        return {"mode": "recent"}
    if _is_root(text):
        return {"mode": "root"}
    raise SsiosError(
        "drive_intent_invalid",
        "Say which Drive folder you mean. A raw folder id is not required.",
        400,
    )


def _is_root(text: str) -> bool:
    folded = text.casefold()
    if any(cue in text for cue in ("根目录", "我的云端硬盘")):
        return True
    if "my drive" in folded or "drive root" in folded:
        return True
    return bool(re.search(r"\broot\b", folded))


def _folder_name(text: str) -> str:
    for pattern in _FOLDER_NAMED:
        found = pattern.search(text)
        if not found:
            continue
        name = " ".join(found.group(1).split()).strip(" .")
        if name and name.casefold() not in {"my", "google", "the", "a"}:
            return name
    return ""


def _keyword(text: str) -> str:
    for pattern in _SEARCH:
        found = pattern.search(text)
        if found:
            return " ".join(found.group(1).split()).strip(" .")
    return ""

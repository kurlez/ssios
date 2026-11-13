"""Let chat find things in Files. Reads only. The bytes of a file never enter a prompt."""

from __future__ import annotations

import re

from ssios.errors import SsiosError

_NOUN_EN = r"files?|photos?|pictures?|images?|pdfs?|documents?|docs?|screenshots?|videos?|recordings?"
_NOUN_ZH = "文件|图片|照片|相片|截图|文档|素材|视频|录音|资料|pdf|PDF"
_ASK_EN = re.compile(rf"\b(find|search|look for|look up|where is|where are|where's|show me|show|list|do i have|have i got|any)\b.*\b({_NOUN_EN})\b", re.IGNORECASE)
_IN_FILES_EN = re.compile(r"\b(in|from|on) (my |the )?(files|library)\b", re.IGNORECASE)
_ASK_ZH = re.compile(rf"(找|搜|查|看看|列出|显示|有没有|有哪些|在哪).*({_NOUN_ZH})|({_NOUN_ZH}).*(在哪|有没有|有哪些|找|搜|查)")
_IN_FILES_ZH = re.compile(r"(Files|文件库|素材库|文件)\s*(里|中|里面)", re.IGNORECASE)
# "Find X" with no word for a file. Files is tried first, and the turn goes elsewhere when nothing matches.
_FIND = re.compile(r"^(please )?(find|search for|look for|look up)\b|帮我找|找一下|找找|搜一下|搜索一下|查找", re.IGNORECASE)
# A request to change a file is workspace work or a Files page action, not a search.
_CHANGE = re.compile(r"\b(edit|modify|delete|remove|rename|create|write|save|upload|move|run)\b|修改|编辑|删除|重命名|新建|创建|写一个|上传|移动|运行", re.IGNORECASE)
_STOP_EN = {
    "find", "search", "look", "for", "up", "where", "is", "are", "show", "me", "list", "do", "i", "have", "got", "any", "my", "the", "a", "an",
    "of", "in", "on", "from", "about", "with", "that", "which", "all", "please", "can", "you", "files", "file", "library", "and", "or", "to", "s",
}
_STOP_ZH = ("帮我", "请", "找一下", "搜一下", "查一下", "看看", "列出", "显示", "有没有", "有哪些", "在哪里", "在哪", "我的", "关于", "相关", "里面", "一下", "那个", "那张", "这个", "所有", "文件库", "素材库", "的", "里", "中", "找", "搜", "查", "吗", "呢", "？", "?", "。", "，", ",")
_GENERIC = {"file", "files", "document", "documents", "doc", "docs", "文件", "资料", "素材", "文档"}


_TAG = re.compile(r"(?:(?<![\w])#|\btag:\s*|\btagged\s+(?:with\s+|as\s+)?|标签(?:是|为|叫|有)?[:：]?\s*)([A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_.\-\u4e00-\u9fff]*)", re.IGNORECASE)
_TAG_ASK = re.compile(r"\btag:|\btagged\b|\btags?\b.*\b(files?|photos?|pictures?|images?|pdfs?|documents?)\b|标签", re.IGNORECASE)


def tag_filter(text: str) -> tuple[list[str], str]:
    """Tags named as #tag, tag:name, tagged name, or 标签name, and the text that is left."""
    raw = " ".join(str(text or "").split())
    names = []
    for found in _TAG.finditer(raw):
        name = re.sub(r"的.*$", "", found.group(1)).casefold()
        if name and name not in names:
            names.append(name)
    rest = _TAG.sub(" ", raw)
    return names, " ".join(rest.split())


def files_request(message: str) -> bool:
    """True when the message asks to find something in Files. A request to change a file is not one."""
    text = " ".join(str(message or "").split())
    if not text or _CHANGE.search(text):
        return False
    if _TAG_ASK.search(text) and tag_filter(text)[0]:
        return True
    return bool(_ASK_EN.search(text) or _IN_FILES_EN.search(text) or _ASK_ZH.search(text) or _IN_FILES_ZH.search(text))


def find_request(message: str) -> bool:
    """A bare request to find something, such as 帮我找一下AMC. It does not say where to look."""
    text = " ".join(str(message or "").split())
    return bool(text) and not _CHANGE.search(text) and bool(_FIND.search(text))


def try_files_first(plane, account_id: str, message: str, route: str, skill: str, earlier: list[dict]) -> bool:
    """True when a turn that was going somewhere else should list Files matches instead.

    That is a bare find request, or a short follow-up to a Files turn, and only when Files has a match.
    A web address, a weather question, and anything with a tool of its own are left alone.
    """
    text = " ".join(str(message or "").split())
    if route not in {"direct", "codex", "vm", "planner", "skill"} or skill == "weather" or "://" in text or _CHANGE.search(text):
        return False
    follow = False
    if route == "direct" and len(text) <= 40 and not text.endswith(("?", "？")):
        for item in reversed(earlier or []):
            if item.get("role") == "user":
                asked = str(item.get("text") or "")
                was = str((item.get("route") or {}).get("route") or "") if isinstance(item.get("route"), dict) else ""
                follow = was == "files" or find_request(asked) or files_request(asked)
                break
    if not find_request(text) and not follow:
        return False
    if not search_terms(text):
        return False
    try:
        return bool(find_files(plane, account_id, text, limit=1))
    except SsiosError:
        return False


def search_terms(message: str) -> list[str]:
    """The words to look for. Question words and the word file itself are dropped. A kind such as photo stays."""
    text = " ".join(str(message or "").split())
    for cue in sorted(_STOP_ZH, key=len, reverse=True):
        text = text.replace(cue, " ")
    terms = []
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*|[一-鿿]+", text):
        folded = word.casefold()
        if folded in _STOP_EN or folded in _GENERIC or word in _GENERIC:
            continue
        if len(word) == 1 and "\u4e00" <= word <= "\u9fff":
            continue
        if folded not in terms:
            terms.append(folded)
    return terms[:8]


_KIND = {
    "image": ("photo", "photos", "picture", "pictures", "image", "images", "screenshot", "screenshots", "图片", "照片", "相片", "截图"),
    "video": ("video", "videos", "视频"),
    "audio": ("recording", "recordings", "录音"),
    "pdf": ("pdf", "pdfs"),
}


def find_files(plane, account_id: str, message: str, *, limit: int = 8) -> list[dict]:
    """Items in every library this account can open, best match first. Trash is left out."""
    from ssios.library import list_items, list_libraries
    from ssios.library_query import _blob
    from ssios.library_search import meaning_ids

    wanted, message = tag_filter(message)
    terms = [term for term in search_terms(message) if term not in {"tag", "tags", "tagged", "标签"}]
    kinds = {kind for kind, words in _KIND.items() if any(term in words for term in terms)}
    words = [term for term in terms if not any(term in group for group in _KIND.values())]
    scored = []
    for library in list_libraries(plane, account_id):
        library_id = str(library["id"])
        try:
            items = list_items(plane, account_id, library_id)
            close = meaning_ids(plane, account_id, library_id, " ".join(words)) if words else []
        except SsiosError:
            continue
        for item in items:
            group = str(item.get("group") or "")
            ext = str(item.get("ext") or "").casefold()
            if kinds and group not in kinds and ext not in kinds:
                continue
            carried = {str(tag).casefold() for tag in item.get("tags") or []}
            if wanted and not all(one in carried for one in wanted):
                continue
            blob = _blob(item)
            # A word that is exactly one of the item's tags counts more than a word found in passing.
            hits = sum(3 if word in carried else 1 for word in words if word in blob)
            item_id = str(item.get("id") or "")
            if words and not hits and item_id not in close:
                continue
            near = (len(close) - close.index(item_id)) / (len(close) + 1) if item_id in close else 0.0
            scored.append((hits + near, str(item.get("modified_at") or item.get("imported_at") or ""), library, item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    found = []
    for _score, _when, library, item in scored[: max(1, limit)]:
        found.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "library": str(library.get("name") or ""),
                "tags": [str(tag) for tag in item.get("tags") or []][:6],
                "description": str(item.get("description") or "")[:200],
                "url": f"/ui/library?library={library['id']}&item={item.get('id')}",
            }
        )
    return found


def files_text(rows: list[dict], lang: str = "en") -> str:
    """A Markdown list with a link to each item on the Files page."""
    zh = lang == "zh"
    if not rows:
        return "Files 里没有找到匹配的文件。没有被分析过的文件只能按文件名和标签找到。" if zh else "Nothing in Files matched. A file that has not been analyzed can only be found by its name and tags."
    lines = [f"在 Files 里找到 {len(rows)} 个：" if zh else f"Found {len(rows)} in Files:", ""]
    for row in rows:
        name = row["name"].replace("[", "(").replace("]", ")")
        line = f"- [{name}]({row['url']})"
        extra = []
        if row.get("library"):
            extra.append(row["library"])
        if row.get("tags"):
            extra.append(", ".join(row["tags"]))
        if extra:
            line += " — " + " · ".join(extra)
        lines.append(line)
        if row.get("description"):
            lines.append("  " + row["description"])
    return "\n".join(lines)


def files_block(rows: list[dict], lang: str = "en") -> dict:
    """The same list as cards for the chat page: one card per item with an Open link."""
    zh = lang == "zh"
    events = []
    for row in rows:
        ext = row["name"].rsplit(".", 1)[-1].upper()[:5] if "." in row["name"] else ""
        meta = " · ".join(part for part in (row.get("library") or "", ", ".join(row.get("tags") or [])) if part)
        events.append({"title": row["name"], "badge": ext, "when": "", "location": "", "meta": meta, "description": row.get("description") or "", "link": row["url"]})
    summary = f"在 Files 里找到 {len(rows)} 个" if zh else f"Found {len(rows)} in Files"
    return {"kind": "files", "header": "Files", "summary": summary, "events": events}


def read_file_note(plane, account_id: str, item_id: str) -> str:
    """What analysis stored about one item: name, tags, description, and text read from it. Not the bytes."""
    from ssios.library import list_items, list_libraries

    for library in list_libraries(plane, account_id):
        for item in list_items(plane, account_id, str(library["id"])):
            if item.get("id") == item_id:
                parts = [
                    "name: " + str(item.get("name") or ""),
                    "tags: " + ", ".join(str(tag) for tag in item.get("tags") or []),
                    "description: " + str(item.get("description") or ""),
                    "text: " + str(item.get("ocr") or "")[:4000],
                ]
                return "\n".join(parts)
    raise SsiosError("library_missing", "That file is not in Files.", 404)

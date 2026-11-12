"""Smart folders. A saved query is computed when the list is drawn. Files are not copied."""

from __future__ import annotations

import hashlib
import re

from ssios.errors import SsiosError
from ssios.library import _now, list_items, metadata, replace_metadata
from ssios.locale_messages import say

_SMART = re.compile(r"smf-[0-9a-f]{8}")
_STAR = re.compile(r"star\s*(>=|<=|>|<|=)\s*([0-5])\Z")
_CMP = {">=": "gte", "<=": "lte", ">": "gt", "<": "lt", "=": "eq"}


def parse_smart_query(text: str) -> dict:
    """One line of AND, OR, and NOT. Mixed AND and OR in one line is rejected."""
    raw = " ".join(str(text or "").split())
    if not raw or len(raw) > 200 or "\n" in str(text or ""):
        raise ValueError("empty")
    if " AND " in raw and " OR " in raw:
        raise ValueError("mixed")
    joiner = "or" if " OR " in raw else "and"
    chunks = raw.split(" OR " if joiner == "or" else " AND ")
    terms = [_term(chunk) for chunk in chunks if chunk.strip()]
    if not terms:
        raise ValueError("empty")
    if len(terms) == 1:
        return terms[0]
    return {"op": joiner, "terms": terms}


def add_smart_folder(plane, account_id: str, library_id: str, name: str, query_text: str, peer_ip: str) -> dict:
    from ssios.library import _audit, _lang

    lang = _lang(plane, account_id)
    title = " ".join(str(name or "").split())[:40]
    if len(title) < 1 or title.startswith(".") or "/" in title:
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400)
    try:
        query = parse_smart_query(query_text)
    except ValueError as exc:
        raise SsiosError("library_rejected", say(lang, "library_rejected"), 400) from exc
    data = metadata(plane, account_id, library_id)
    folder_id = "smf-" + hashlib.sha256(f"{library_id}:{title}:{_now()}".encode()).hexdigest()[:8]
    while any(isinstance(row, dict) and row.get("id") == folder_id for row in data.get("smart_folders") or []):
        folder_id = "smf-" + hashlib.sha256(folder_id.encode()).hexdigest()[:8]
    row = {"id": folder_id, "name": title, "query": query, "text": " ".join(str(query_text).split())[:200]}
    data.setdefault("smart_folders", []).append(row)
    replace_metadata(plane, account_id, library_id, data)
    _audit(plane, account_id, peer_ip, "library.smart", folder_id, "ok", 0)
    return row


def smart_items(plane, account_id: str, library_id: str, smart_id: str) -> list[dict]:
    if not _SMART.fullmatch(smart_id):
        raise SsiosError("library_missing", say(_lang_of(plane, account_id), "library_missing"), 404)
    data = metadata(plane, account_id, library_id)
    row = next((item for item in data.get("smart_folders") or [] if isinstance(item, dict) and item.get("id") == smart_id), None)
    if row is None:
        raise SsiosError("library_missing", say(_lang_of(plane, account_id), "library_missing"), 404)
    return [item for item in list_items(plane, account_id, library_id) if matches(item, row.get("query"))]


def matches(item: dict, query) -> bool:
    if not isinstance(query, dict):
        return False
    op = query.get("op")
    if op == "and":
        terms = query.get("terms") if isinstance(query.get("terms"), list) else []
        return bool(terms) and all(matches(item, term) for term in terms)
    if op == "or":
        terms = query.get("terms") if isinstance(query.get("terms"), list) else []
        return any(matches(item, term) for term in terms)
    if op == "not":
        return not matches(item, query.get("term") if isinstance(query.get("term"), dict) else {})
    field = str(query.get("field") or "")
    if field == "name":
        return str(query.get("value") or "").casefold() in str(item.get("name") or "").casefold()
    if field == "tag":
        return str(query.get("value") or "") in [str(tag) for tag in item.get("tags") or []]
    if field == "ext":
        return str(item.get("ext") or "").casefold() == str(query.get("value") or "").casefold()
    if field == "star":
        return _star(int(item.get("star") or 0), str(query.get("cmp") or "eq"), int(query.get("value") or 0))
    if field == "text":
        return str(query.get("value") or "").casefold() in _blob(item)
    return False


def _blob(item: dict) -> str:
    parts = [
        str(item.get("name") or ""),
        str(item.get("annotation") or ""),
        str(item.get("description") or ""),
        str(item.get("ocr") or ""),
        " ".join(str(tag) for tag in item.get("tags") or []),
    ]
    return " ".join(parts).casefold()


def _star(actual: int, cmp: str, expected: int) -> bool:
    if cmp == "gte":
        return actual >= expected
    if cmp == "lte":
        return actual <= expected
    if cmp == "gt":
        return actual > expected
    if cmp == "lt":
        return actual < expected
    return actual == expected


def _term(chunk: str) -> dict:
    text = chunk.strip()
    if text.startswith("NOT "):
        return {"op": "not", "term": _positive(text[4:].strip())}
    return _positive(text)


def _positive(text: str) -> dict:
    if text.startswith("tag:"):
        value = text[4:].strip()
        if not value:
            raise ValueError("tag")
        return {"field": "tag", "value": value[:40]}
    if text.startswith("name:"):
        value = text[5:].strip()
        if not value:
            raise ValueError("name")
        return {"field": "name", "value": value[:80]}
    if text.startswith("ext:"):
        value = text[4:].strip().lstrip(".")
        if not value:
            raise ValueError("ext")
        return {"field": "ext", "value": value[:8]}
    star = _STAR.fullmatch(text)
    if star:
        return {"field": "star", "cmp": _CMP[star.group(1)], "value": int(star.group(2))}
    if not text:
        raise ValueError("empty")
    return {"field": "text", "value": text[:80]}


def _lang_of(plane, account_id: str) -> str:
    from ssios.library import _lang

    return _lang(plane, account_id)

"""A short-lived question waiting for one missing value.

A skill or connector turn that asks for a location, date, count, or folder
keeps that ask on the conversation. The next message can fill it. The ask
expires after about 10 minutes or three turns that do not answer it.
"""

from __future__ import annotations

import re
import time

TTL_SECONDS = 600
TURNS = 3

_CANCEL = ("算了", "取消", "不用了", "never mind", "cancel")
_OTHER_TOPIC = ("邮件", "邮箱", "收件箱", "日历", "日程", "网盘", "云端硬盘", "文件夹")
_DATE = re.compile(
    r"^(?:明天的?|今天的?|后天的?|下周|这周|tomorrow|today|tonight|next week|this week)$",
    re.IGNORECASE,
)
_COUNT = re.compile(r"^(?:[1-9]\d?|100|[一二两三四五六七八九十]{1,3})\s*(?:封|个|条)?$")
_PLACE = re.compile(r"^(?:[A-Za-z]{3,}(?:[ .'-][A-Za-z]+)*(?:,\s*[A-Za-z]{2,})?|[\u4e00-\u9fff]{2,8})$")
_DATE_WORDS = {"明天", "明天的", "今天", "今天的", "后天", "后天的", "下周", "这周"}
_STATED = re.compile(
    r"(?:我在|我住在|i live in|i'm in|i am in)\s*(.+)$",
    re.IGNORECASE,
)
_NOT_A_PLACE = set("想看做说听写吃走读")


def fresh(
    skill: str,
    slot: str,
    *,
    origin: str = "",
    choices: list[str] | None = None,
    candidates: list[dict] | None = None,
    query: str = "",
    now: float | None = None,
) -> dict:
    return {
        "skill": skill,
        "slot": slot,
        "origin": " ".join(str(origin or "").split())[:240],
        "choices": [str(item) for item in (choices or []) if str(item)][:6],
        "candidates": [item for item in (candidates or []) if isinstance(item, dict)][:6],
        "query": " ".join(str(query or "").split())[:80],
        "rounds": 1 if choices else 0,
        "expires_at": (time.time() if now is None else now) + TTL_SECONDS,
        "turns_left": TURNS,
    }


def consume(slot: dict | None, message: str, now: float | None = None) -> dict:
    """Decide what this message does to a pending ask. The slot itself is not written."""
    if not isinstance(slot, dict) or not slot.get("skill"):
        return {"action": "miss"}
    clock = time.time() if now is None else now
    if float(slot.get("expires_at") or 0) <= clock or int(slot.get("turns_left") or 0) <= 0:
        return {"action": "clear"}
    if _topic_changed(message, slot):
        return {"action": "clear"}
    value = _fill(message, slot)
    if value:
        picked = _candidate(value, list(slot.get("candidates") or []))
        bare = " ".join(str(message or "").split()).casefold()
        query_city = str(slot.get("query") or "").split(",", 1)[0].strip().casefold()
        assumed = bool(picked and bare == query_city and len(slot.get("candidates") or []) > 1)
        if isinstance(picked, dict) and assumed:
            picked = dict(picked)
            picked["assumed"] = True
        return {
            "action": "resume",
            "skill": str(slot.get("skill") or ""),
            "slot": str(slot.get("slot") or ""),
            "value": value,
            "origin": str(slot.get("origin") or ""),
            "composed": _compose(slot, value),
            "candidate": picked,
            "rounds": int(slot.get("rounds") or 0),
            "assumed": assumed,
        }
    left = int(slot.get("turns_left") or 0) - 1
    if left <= 0:
        return {"action": "clear"}
    updated = dict(slot)
    updated["turns_left"] = left
    return {"action": "keep", "slot": updated}


def _topic_changed(message: str, slot: dict) -> bool:
    text = " ".join(str(message or "").split())
    folded = text.casefold()
    if any(word in text for word in _CANCEL[:3]) or any(word in folded for word in _CANCEL[3:]):
        return True
    if any(word in text for word in _OTHER_TOPIC) or re.search(r"\b(gmail|inbox|calendar|google drive)\b", folded):
        return True
    from ssios.skill_broker import match_skill

    matched = match_skill(text)
    skill = str(slot.get("skill") or "")
    if matched and matched != skill:
        return True
    if matched == "weather" and skill == "weather" and not _place(text):
        return True
    if text.endswith(("?", "？")) or "吗" in text or folded.startswith(("what ", "why ", "how ", "who ")):
        return True
    return False


def _fill(message: str, slot: dict) -> str:
    kind = str(slot.get("slot") or "")
    if kind == "place":
        return _choice(message, list(slot.get("choices") or []), query=str(slot.get("query") or ""))
    if kind == "location":
        return _place(message)
    if kind == "date":
        text = " ".join(str(message or "").split())
        return text if _DATE.fullmatch(text) else ""
    if kind == "count":
        text = " ".join(str(message or "").split())
        return text if _COUNT.fullmatch(text) else ""
    if kind == "folder":
        text = " ".join(str(message or "").split())
        if text and len(text) <= 40 and _PLACE.fullmatch(text):
            return text
    return ""


def _place(message: str) -> str:
    text = " ".join(str(message or "").split())
    found = _STATED.search(text)
    if found:
        value = found.group(1).strip(" .")
        if value and value[0] not in _NOT_A_PLACE:
            return value[:80]
        return ""
    if text in _DATE_WORDS:
        return ""
    if _PLACE.fullmatch(text):
        return text
    # A bare place in another script, such as Москва or 東京. A question was already cleared.
    if any(ord(char) > 127 for char in text) and re.fullmatch(r"(?u)\w[\w .,'’-]{0,39}", text):
        return text[:80]
    return ""


def _choice(message: str, choices: list[str], query: str = "") -> str:
    text = " ".join(str(message or "").split())
    if re.fullmatch(r"[1-9]", text):
        index = int(text) - 1
        if index < len(choices):
            return choices[index]
    folded = text.casefold()
    for choice in choices:
        if folded == choice.casefold():
            return choice
    city, regions = _split_label(text)
    if city and regions:
        for choice in choices:
            choice_city, choice_regions = _split_label(choice)
            if choice_city.casefold() == city.casefold() and _region_hit(regions, choice_regions):
                return choice
    if city and not regions:
        exact = [choice for choice in choices if _split_label(choice)[0].casefold() == city.casefold()]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1 and query and city.casefold() == query.split(",", 1)[0].strip().casefold():
            return exact[0]
    region_hits = [choice for choice in choices if _region_hit([text], _split_label(choice)[1])]
    if len(region_hits) == 1:
        return region_hits[0]
    if len(region_hits) > 1 and query:
        wanted = query.split(",", 1)[0].strip().casefold()
        named = [choice for choice in region_hits if _split_label(choice)[0].casefold() == wanted]
        if len(named) == 1:
            return named[0]
    return ""


def _split_label(value: str) -> tuple[str, list[str]]:
    parts = [part.strip() for part in str(value or "").split(",") if part.strip()]
    if not parts:
        return "", []
    return parts[0], parts[1:]


def _region_hit(wanted: list[str], have: list[str]) -> bool:
    left = {part.casefold() for part in wanted}
    right = {part.casefold() for part in have}
    return bool(left and left & right)


def _candidate(label: str, candidates: list[dict]) -> dict | None:
    for item in candidates:
        if str(item.get("label") or "").casefold() == str(label or "").casefold():
            return dict(item)
    return None


def _compose(slot: dict, value: str) -> str:
    origin = str(slot.get("origin") or "").strip()
    kind = str(slot.get("slot") or "")
    if kind == "folder":
        return f"{origin} folder {value}".strip()
    if kind == "date":
        return f"{value} {origin}".strip()
    if kind == "count":
        return f"{origin} {value}".strip()
    return value

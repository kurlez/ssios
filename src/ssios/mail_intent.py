"""Gmail search built from the sentence. The query stays operators, not the user's words."""

from __future__ import annotations

import re

_SENDERS = re.compile(r"\b(senders?|who emailed|who sent|from whom)\b", re.IGNORECASE)
_UNREAD = re.compile(r"\bunread\b", re.IGNORECASE)
_WEEK = re.compile(r"\b(last 7 days|past week|this week)\b", re.IGNORECASE)
_CN_WEEK = ("最近7天", "最近七天", "过去7天", "过去一周", "最近一周")


def plan_mail(message: str) -> dict:
    """``in:inbox`` and the latest 50. A named week adds ``newer_than:7d``. Senders are counted later."""
    text = str(message or "")
    folded = text.casefold()
    view = "senders" if ("发件人" in text or "寄件人" in text or _SENDERS.search(folded)) else "list"
    parts = ["in:inbox"]
    if "未读" in text or _UNREAD.search(folded):
        parts.append("is:unread")
    if any(cue in text for cue in _CN_WEEK) or _WEEK.search(folded):
        parts.append("newer_than:7d")
    elif "今天" in text or re.search(r"\btoday\b", folded):
        parts.append("newer_than:1d")
    return {"q": " ".join(parts), "view": view}

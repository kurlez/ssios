"""User-facing sentences for skill replies. The rest of the repository stays English.

Chinese text here is localization data, not source comments.
"""

from __future__ import annotations

import re

MESSAGES = {
    "en": {
        "which_city": "Which city? For example, Seattle or Shanghai.",
        "change_city": "Which city should I use instead?",
        "ambiguous": "{place} matches more than one place. Reply with one: {shown}.",
        "nearby": "No exact match for {place}. Nearby places: {shown}.",
        "city_not_found": "No matching city was found.",
        "city_not_found_hint": "No city matched that name. Try a city and a region, such as Bellevue, WA. Nothing was fetched.",
        "say_city": "Say a city, or save a default city in Settings.",
        "using": "Using {label}.",
        "using_elsewhere": "Using {label}. Say another place if you meant elsewhere.",
        "using_largest": "Using {label}. Several places still match, so this is the largest.",
        "using_saved": "Using {label}. Say change city if you meant somewhere else.",
        "saved_default": "Saved {label} as your default city.",
        "yes_web": "Yes. Weather is read on this computer.",
        "weather_line": "{label}: {temp}°{unit}, {condition}. Wind {wind} km/h.",
        "condition_clear": "Clear",
        "condition_mainly_clear": "Mainly clear",
        "condition_partly_cloudy": "Partly cloudy",
        "condition_overcast": "Overcast",
        "condition_fog": "Fog",
        "condition_drizzle": "Drizzle",
        "condition_rain": "Rain",
        "condition_snow": "Snow",
        "condition_showers": "Showers",
        "condition_thunderstorm": "Thunderstorm",
        "condition_unknown": "Unknown",
        "reply_instruction": "Reply in English.",
        "calendar_clarify": "Which event and what time should I put on your calendar?",
    },
    "zh": {
        "which_city": "哪个城市？例如西雅图或上海。",
        "change_city": "要改成哪个城市？",
        "ambiguous": "{place} 对应多个地方。请选一个：{shown}。",
        "nearby": "没有和 {place} 完全一致的结果。附近有：{shown}。",
        "city_not_found": "没有找到这个城市。",
        "city_not_found_hint": "没有匹配的城市。试着写城市和地区，例如 Bellevue, WA。没有获取任何内容。",
        "say_city": "请说一个城市，或在设置里保存默认城市。",
        "using": "使用 {label}。",
        "using_elsewhere": "使用 {label}。如果不是这里，请再说一个地方。",
        "using_largest": "使用 {label}。还有几个同名地点，所以用了最大的。",
        "using_saved": "使用 {label}。要换城市可以说「换个城市」。",
        "saved_default": "已把 {label} 存为默认城市。",
        "yes_web": "可以。天气是在这台电脑上读取的。",
        "weather_line": "{label}：{temp}°{unit}，{condition}。风速 {wind} km/h。",
        "condition_clear": "晴",
        "condition_mainly_clear": "大部晴朗",
        "condition_partly_cloudy": "多云",
        "condition_overcast": "阴",
        "condition_fog": "雾",
        "condition_drizzle": "毛毛雨",
        "condition_rain": "雨",
        "condition_snow": "雪",
        "condition_showers": "阵雨",
        "condition_thunderstorm": "雷暴",
        "condition_unknown": "未知",
        "reply_instruction": "用中文回答。",
        "calendar_clarify": "要改日历的话，请说时间和事件。",
    },
}

_CONDITIONS = {
    0: "condition_clear",
    1: "condition_mainly_clear",
    2: "condition_partly_cloudy",
    3: "condition_overcast",
    45: "condition_fog",
    48: "condition_fog",
    51: "condition_drizzle",
    61: "condition_rain",
    63: "condition_rain",
    65: "condition_rain",
    71: "condition_snow",
    73: "condition_snow",
    80: "condition_showers",
    95: "condition_thunderstorm",
}


def say(lang: str, key: str, **values) -> str:
    table = MESSAGES.get(lang) or MESSAGES["en"]
    template = table.get(key) or MESSAGES["en"].get(key) or key
    return template.format(**values)


def condition_name(lang: str, code: int | None) -> str:
    key = _CONDITIONS.get(code if isinstance(code, int) else -1, "condition_unknown")
    return say(lang, key)


def reply_language(text: str, preferred: str = "en") -> str:
    """CJK in the latest message is Chinese. A bare place uses the account language."""
    from ssios.skill_broker import place_only

    message = " ".join(str(text or "").split())
    choice = preferred if preferred in {"en", "zh"} else "en"
    if re.search(r"[\u4e00-\u9fff]", message):
        return "zh"
    if not message or place_only(message):
        return choice
    if re.search(r"[A-Za-z]", message):
        return "en"
    return choice


def wants_language_change(text: str) -> str:
    folded = str(text or "").casefold()
    if any(phrase in text for phrase in ("以后用中文", "用中文回答", "请用中文")) or "answer in chinese" in folded or "reply in chinese" in folded:
        return "zh"
    if any(phrase in text for phrase in ("以后用英文", "用英文回答", "请用英文")) or "answer in english" in folded or "reply in english" in folded:
        return "en"
    return ""

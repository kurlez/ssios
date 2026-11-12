"""Resolved places for one account. The default city is also written to preferences.md."""

from __future__ import annotations

import re

from ssios.skill_broker import _fold_place
from ssios.util import read_json, write_json


def alias_key(value: str) -> str:
    folded = _fold_place(value)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", folded).strip()


def _load(home) -> dict:
    data = read_json(home / "weather-places.json", {"aliases": {}, "uses": {}})
    if not isinstance(data, dict):
        data = {"aliases": {}, "uses": {}}
    if not isinstance(data.get("aliases"), dict):
        data["aliases"] = {}
    if not isinstance(data.get("uses"), dict):
        data["uses"] = {}
    return data


def _save(home, data: dict) -> None:
    write_json(home / "weather-places.json", {"aliases": data.get("aliases") or {}, "uses": data.get("uses") or {}})


def cached_place(home, phrase: str) -> dict | None:
    key = alias_key(phrase)
    if not key:
        return None
    found = _load(home)["aliases"].get(key)
    if isinstance(found, dict) and found.get("latitude") is not None and found.get("longitude") is not None:
        return dict(found)
    return None


def remember_alias(home, phrases: list[str], record: dict) -> None:
    data = _load(home)
    stored = {
        "name": str(record.get("name") or ""),
        "admin1": str(record.get("admin1") or ""),
        "country": str(record.get("country") or ""),
        "country_code": str(record.get("country_code") or ""),
        "latitude": record.get("latitude"),
        "longitude": record.get("longitude"),
        "label": str(record.get("label") or record.get("name") or ""),
    }
    if stored["latitude"] is None or stored["longitude"] is None:
        return
    for phrase in phrases:
        key = alias_key(phrase)
        if key:
            data["aliases"][key] = stored
    label_key = alias_key(stored["label"])
    if label_key:
        data["aliases"][label_key] = stored
    _save(home, data)


def note_use(home, record: dict) -> int:
    key = alias_key(str(record.get("label") or record.get("name") or ""))
    if not key:
        return 0
    data = _load(home)
    count = int(data["uses"].get(key) or 0) + 1
    data["uses"][key] = count
    _save(home, data)
    return count


def clear_uses(home) -> None:
    data = _load(home)
    data["uses"] = {}
    _save(home, data)

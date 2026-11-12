"""A chosen city and a stated preference are written to account memory."""

from __future__ import annotations

import json

from ssios.account_memory import _read
from ssios.scope import AccountScope
from support import PlaneTest
from test_ui import _form, _get

PASSWORD = "correct-horse-battery"
BELLEVUE = [
    {"name": "Bellevue", "admin1": "Washington", "country": "United States", "country_code": "US", "latitude": 47.6, "longitude": -122.2, "population": 151854, "feature_code": "PPL"},
    {"name": "Bellevue", "admin1": "Nebraska", "country": "United States", "country_code": "US", "latitude": 41.1, "longitude": -95.9, "population": 80000, "feature_code": "PPL"},
]
SEATTLE = [
    {"name": "Seattle", "admin1": "Washington", "country": "United States", "country_code": "US", "latitude": 47.6, "longitude": -122.3, "population": 737015, "feature_code": "PPLA"},
]


class PlaceMemoryTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.plane.accounts.add_admin("owner", PASSWORD)
        status, page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": PASSWORD})
        self.assertEqual(status, 200, page)
        self.cookie = cookie.split(";", 1)[0]
        self.account_id = next(item["id"] for item in self.plane.accounts.public_list() if item["username"] == "owner")
        self.plane.skills.set_enabled(True)
        self.plane.skills.resolver = lambda host: [__import__("ipaddress").ip_address("1.1.1.1")]
        self.plane.skills.opener = self._open

    def _csrf(self) -> str:
        _status, page = _get(self.base, "/ui/chat", self.cookie)
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def _send(self, message: str, conversation: str = "") -> str:
        body = {"csrf": self._csrf(), "message": message}
        if conversation:
            body["conversation"] = conversation
        status, page, _cookie = _form(self.base, "/ui/chat/send", body, cookie=self.cookie)
        self.assertIn(status, (200, 404), page)
        return page

    def _prefs(self) -> str:
        scope = AccountScope(self.plane, self.plane.accounts.get(self.account_id))
        return _read(scope.memory(), scope.project_id(), "preferences.md")

    def _open(self, url, headers=None):
        del headers
        if "geocoding-api" in url and "Bellevue" in url:
            return 200, {}, json.dumps({"results": BELLEVUE}).encode()
        if "geocoding-api" in url and "Seattle" in url:
            return 200, {}, json.dumps({"results": SEATTLE}).encode()
        if "geocoding-api" in url:
            return 200, {}, b'{"results":[]}'
        if "api.open-meteo.com" in url:
            return 200, {}, json.dumps({"current": {"temperature_2m": 12, "weather_code": 1, "wind_speed_10m": 10}}).encode()
        return 500, {}, b""

    def test_a_choice_is_saved_and_the_next_question_uses_it(self) -> None:
        asked = self._send("What's the weather in Bellevue?")
        self.assertIn("Reply with one", asked)
        self.assertIn("Bellevue, WA", asked)
        conversation = asked.split('class="conversation">', 1)[1].split("<", 1)[0]
        chosen = self._send("1", conversation)
        self.assertIn("12°C", chosen)
        self.assertIn("Saved", chosen)
        self.assertIn("Bellevue", self._prefs())
        self.assertIn("47.6", self._prefs())
        again = self._send("今天天气怎么样")
        self.assertIn("使用", again)
        self.assertIn("12°C", again)
        self.assertNotIn("哪个城市？", again)
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("memory.auto", audit)

    def test_english_and_chinese_weather_prompts(self) -> None:
        english = self._send("What's the weather?")
        self.assertIn("Which city?", english)
        chinese = self._send("今天天气怎么样")
        self.assertIn("哪个城市？", chinese)
        missing = self._send("What's the weather in Nowhereville?")
        self.assertIn("No matching city was found.", missing)
        self.assertIn("No city matched that name", missing)
        missing_zh = self._send("今天天气 in Nowhereville")
        self.assertIn("没有找到这个城市", missing_zh)
        self.assertIn("没有匹配的城市", missing_zh)

    def test_a_preference_a_correction_and_a_repeated_city(self) -> None:
        liked = self._send("我喜欢红茶")
        self.assertIn("Saved to memory (preferences.md).", liked)
        self.assertIn("prefers: 红茶", self._prefs())
        first = self._send("What's the weather in Bellevue?")
        conv = first.split('class="conversation">', 1)[1].split("<", 1)[0]
        self._send("Bellevue, WA", conv)
        seattle = self._send("What's the weather in Seattle?")
        self.assertIn("12°C", seattle)
        self.assertNotIn("Saved Seattle", seattle)
        again = self._send("What's the weather in Seattle?")
        self.assertIn("Saved", again)
        self.assertIn("Seattle", self._prefs())
        fixed = self._send("不是这个, 是Bellevue WA")
        self.assertIn("12°C", fixed)
        self.assertIn("Bellevue", self._prefs())
        self.assertIn("存为默认城市", fixed)

    def test_settings_language_and_units_sync_to_memory(self) -> None:
        status, page, _cookie = _form(
            self.base,
            "/ui/settings",
            {"csrf": self._csrf(), "language": "zh", "units": "celsius"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, page)
        self.assertIn("Language: zh", self._prefs())
        self.assertIn("Units: celsius", self._prefs())
        self.assertEqual(self.plane.skills.account_prefs(self.account_id)["language"], "zh")

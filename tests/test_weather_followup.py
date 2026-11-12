"""Weather follow-ups stay on the skill. The local model is not asked for a live forecast."""

from __future__ import annotations

import json

from ssios.google_connector import GoogleConnector, GoogleEndpoints
from ssios.secret_store import MemorySecretStore
from ssios.skill_broker import geocode_attempts
from support import PlaneTest
from test_skills import PASSWORD, _Guest, _public
from test_ui import _form, _get


BELLEVUE = [
    {
        "name": "Bellevue",
        "admin1": "Washington",
        "country": "United States",
        "country_code": "US",
        "latitude": 47.6,
        "longitude": -122.2,
        "population": 151854,
        "feature_code": "PPL",
    },
    {
        "name": "Bellevue",
        "admin1": "Nebraska",
        "country": "United States",
        "country_code": "US",
        "latitude": 41.1,
        "longitude": -95.9,
        "population": 5000,
        "feature_code": "PPL",
    },
]


class WeatherFollowupTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.guest = _Guest()
        self.plane.chat_guest = self.guest
        self.plane._google = GoogleConnector(self.plane, MemorySecretStore(), GoogleEndpoints())
        self.plane.accounts.add_admin("owner", PASSWORD)
        status, page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": PASSWORD})
        self.assertEqual(status, 200, page)
        self.cookie = cookie.split(";", 1)[0]
        self.account_id = next(item["id"] for item in self.plane.accounts.public_list() if item["username"] == "owner")
        self.seen: list[str] = []
        self.plane.skills.resolver = _public
        self.plane.skills.opener = self._bellevue_open

    def _csrf(self) -> str:
        _status, page = _get(self.base, "/ui/chat", self.cookie)
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def _send(self, message: str, conversation: str = "") -> tuple[int, str, str]:
        body = {"csrf": self._csrf(), "message": message}
        if conversation:
            body["conversation"] = conversation
        return _form(self.base, "/ui/chat/send", body, cookie=self.cookie)

    def _conversation(self, page: str) -> str:
        return page.split('class="conversation">', 1)[1].split("<", 1)[0]

    def _slot(self, conversation: str) -> dict | None:
        path = self.plane.accounts.home(self.account_id) / "chat" / f"{conversation}.json"
        return json.loads(path.read_text(encoding="utf-8")).get("pending_slot")

    def test_the_screenshot_transcript_stays_on_weather(self) -> None:
        self.plane.skills.set_enabled(True)
        self.plane.skills.opener = self._bellevue_open
        streamed: list[str] = []

        def stream(messages, *, cancel_event, deadline):
            del messages, cancel_event, deadline
            streamed.append("I don't have real-time weather data access")
            yield "I don't have real-time weather data access"

        self.plane.llm.stream_chat = stream
        status, asked, _cookie = self._send("今天天气怎么样？")
        self.assertEqual(status, 200, asked)
        self.assertIn("哪个城市？", asked)
        self.assertIn("Route: skill", asked)
        conversation = self._conversation(asked)
        self.assertEqual(self._slot(conversation)["slot"], "location")
        status, named, _cookie = self._send("Bellevue Washington USA", conversation)
        self.assertEqual(status, 200, named)
        self.assertIn("12°C", named)
        self.assertIn("Washington", named)
        self.assertNotIn("Nebraska", named)
        self.assertNotIn("skill_denied", named)
        self.assertNotIn("city_not_found", named)
        self.assertNotIn("real-time weather data access", named)
        self.assertIn("Route: skill", named)
        self.assertTrue(any(url.endswith("name=Bellevue") for url in self.seen))
        self.assertFalse(any(url.endswith("name=Bellevue%20Washington%20USA") for url in self.seen))
        status, refined, _cookie = self._send("Bellevue, WA", conversation)
        self.assertEqual(status, 200, refined)
        self.assertIn("12°C", refined)
        self.assertIn("Washington", refined)
        self.assertIn("Route: skill", refined)
        self.assertNotIn("Route: direct", refined)
        self.assertNotIn("real-time weather data access", refined)
        self.assertEqual(streamed, [])
        status, capable, _cookie = self._send("可以访问网络获得天气信息吗？", conversation)
        self.assertEqual(status, 200, capable)
        self.assertIn("可以。天气是在这台电脑上读取的。", capable)
        self.assertIn("12°C", capable)
        self.assertIn("Bellevue", capable)
        self.assertNotIn("real-time weather data access", capable)
        self.assertNotIn("skill_denied", capable)
        self.assertEqual(streamed, [])
        self.assertEqual(self.guest.prompts, [])

    def test_a_failed_city_keeps_the_question_for_the_next_answer(self) -> None:
        self.plane.skills.set_enabled(True)
        self.plane.skills.opener = self._bellevue_open
        streamed: list[str] = []

        def stream(messages, *, cancel_event, deadline):
            del messages, cancel_event, deadline
            streamed.append("stream")
            yield "I don't have real-time weather data access"

        self.plane.llm.stream_chat = stream
        _status, asked, _cookie = self._send("今天天气怎么样")
        conversation = self._conversation(asked)
        status, missed, _cookie = self._send("Nowhereville", conversation)
        self.assertIn(status, (200, 404), missed)
        self.assertIn("city_not_found", missed)
        self.assertIn("没有匹配的城市", missed)
        self.assertNotIn("skill_denied", missed)
        self.assertNotIn("That site is not allowed", missed)
        self.assertEqual(self._slot(conversation)["skill"], "weather")
        self.assertEqual(self._slot(conversation)["slot"], "location")
        status, fixed, _cookie = self._send("Bellevue, WA", conversation)
        self.assertEqual(status, 200, fixed)
        self.assertIn("12°C", fixed)
        self.assertIn("Washington", fixed)
        self.assertIn("weather skill continues", fixed)
        self.assertNotIn("real-time weather data access", fixed)
        self.assertNotIn("Route: direct", fixed)
        self.assertEqual(streamed, [])
        self.assertIsNone(self._slot(conversation))

    def test_a_region_miss_offers_nearby_places(self) -> None:
        self.plane.skills.set_enabled(True)
        self.plane.skills.opener = self._bellevue_open
        result = self.plane.skills.run(self.account_id, "weather", peer_ip="127.0.0.1", skill="weather", place="Bellevue, ZZ")
        need = result["need"]
        self.assertEqual(need["slot"], "place")
        self.assertIn("Nearby", need["prompt"])
        self.assertIn("Bellevue, WA", need["choices"])
        self.assertNotIn("skill_denied", need["prompt"])

    def test_city_token_is_first_and_a_short_prefix_is_a_fallback(self) -> None:
        names = [name for name, _choose in geocode_attempts("Bellevue Washington USA")]
        self.assertEqual(names[0], "Bellevue")
        self.assertLess(names.index("Bellevue"), names.index("Bellevue Washington USA"))
        self.assertIn("Bellevue Washington", names)

    def _bellevue_open(self, url: str):
        self.seen.append(url)
        if "geocoding-api.open-meteo.com" in url:
            if "name=Nowhere" in url or "name=Bellevue%20" in url:
                return 200, {}, b'{"results":[]}'
            if "name=Bellevue" in url:
                return 200, {}, json.dumps({"results": BELLEVUE}).encode()
            return 200, {}, b'{"results":[]}'
        if "api.open-meteo.com" in url:
            body = json.dumps({"current": {"temperature_2m": 12, "weather_code": 1, "wind_speed_10m": 10}}).encode()
            return 200, {}, body
        return 500, {}, b""

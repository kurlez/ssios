"""Place choice stays on the named city and does not ask the same question twice."""

from __future__ import annotations

import json

from ssios.pending_slot import consume, fresh
from ssios.skill_broker import _choose_place, geocode_attempts, split_place
from support import PlaneTest
from test_ui import _form


PASSWORD = "correct-horse-battery"

SEATTLE = [
    {"name": "Seattle", "admin1": "Washington", "country": "United States", "country_code": "US", "latitude": 47.6, "longitude": -122.3, "population": 737015, "feature_code": "PPLA2"},
    {"name": "Seattle", "admin1": "Jalisco", "country": "Mexico", "country_code": "MX", "latitude": 20.8, "longitude": -103.5, "population": 1200, "feature_code": "PPL"},
    {"name": "Seattle Hill", "admin1": "Washington", "country": "United States", "country_code": "US", "latitude": 47.8, "longitude": -122.1, "population": 9000, "feature_code": "PPL"},
    {"name": "Seattle Heights", "admin1": "Washington", "country": "United States", "country_code": "US", "latitude": 47.7, "longitude": -122.2, "population": 3000, "feature_code": "PPL"},
]
PORTLAND = [
    {"name": "Portland", "admin1": "Oregon", "country": "United States", "country_code": "US", "latitude": 45.5, "longitude": -122.6, "population": 652503, "feature_code": "PPLA"},
    {"name": "Portland", "admin1": "Maine", "country": "United States", "country_code": "US", "latitude": 43.6, "longitude": -70.2, "population": 68408, "feature_code": "PPLA"},
]
SPRINGFIELD = [
    {"name": "Springfield", "admin1": "Missouri", "country": "United States", "country_code": "US", "latitude": 37.2, "longitude": -93.2, "population": 169176, "feature_code": "PPLA"},
    {"name": "Springfield", "admin1": "Massachusetts", "country": "United States", "country_code": "US", "latitude": 42.1, "longitude": -72.5, "population": 155929, "feature_code": "PPLA"},
    {"name": "Springfield", "admin1": "Illinois", "country": "United States", "country_code": "US", "latitude": 39.8, "longitude": -89.6, "population": 114394, "feature_code": "PPLA"},
    {"name": "Springfield", "admin1": "Ohio", "country": "United States", "country_code": "US", "latitude": 39.9, "longitude": -83.8, "population": 58662, "feature_code": "PPLA"},
]
BELLEVUE = [
    {"name": "Bellevue", "admin1": "Washington", "country": "United States", "country_code": "US", "latitude": 47.6, "longitude": -122.2, "population": 151854, "feature_code": "PPL"},
    {"name": "Bellevue", "admin1": "Nebraska", "country": "United States", "country_code": "US", "latitude": 41.1, "longitude": -95.9, "population": 5000, "feature_code": "PPL"},
]
SHANGHAI = [
    {"name": "上海", "admin1": "Shanghai", "country": "China", "country_code": "CN", "latitude": 31.2, "longitude": 121.5, "population": 24800000, "feature_code": "PPLA"},
    {"name": "上海县", "admin1": "Shanghai", "country": "China", "country_code": "CN", "latitude": 31.0, "longitude": 121.2, "population": 1000, "feature_code": "PPL"},
]


class ChoosePlaceTest(PlaneTest):
    def test_exact_and_dominant_places_are_chosen_without_another_question(self) -> None:
        hit, ambiguous, note = _choose_place("Seattle", SEATTLE)
        self.assertEqual(ambiguous, [])
        self.assertEqual(hit["admin1"], "Washington")
        self.assertIn("Using Seattle, WA, US", note)
        self.assertNotIn("Seattle Hill", note)
        narrowed, ambiguous, note = _choose_place("Seattle, WA", SEATTLE)
        self.assertEqual(ambiguous, [])
        self.assertEqual(narrowed["admin1"], "Washington")
        self.assertEqual(note, "")
        hit, ambiguous, note = _choose_place("Bellevue", BELLEVUE)
        self.assertEqual(ambiguous, [])
        self.assertEqual(hit["admin1"], "Washington")
        self.assertIn("Using Bellevue, WA, US", note)
        hit, ambiguous, note = _choose_place("Portland", PORTLAND)
        self.assertIsNone(hit)
        self.assertEqual([item["admin1"] for item in ambiguous], ["Oregon", "Maine"])
        hit, ambiguous, note = _choose_place("Springfield", SPRINGFIELD)
        self.assertIsNone(hit)
        self.assertEqual(ambiguous[0]["admin1"], "Missouri")
        again, ambiguous, note = _choose_place("Springfield", SPRINGFIELD, asked=True)
        self.assertEqual(ambiguous, [])
        self.assertEqual(again["admin1"], "Missouri")
        self.assertIn("largest", note)
        hit, ambiguous, note = _choose_place("上海", SHANGHAI)
        self.assertEqual(ambiguous, [])
        self.assertEqual(hit["name"], "上海")
        self.assertEqual(note, "")

    def test_a_listed_choice_is_not_geocoded_again(self) -> None:
        self.plane.skills.set_enabled(True)
        self.plane.skills.resolver = lambda host: [__import__("ipaddress").ip_address("1.1.1.1")]
        self.seen: list[str] = []

        def opener(url, headers=None):
            del headers
            self.seen.append(url)
            if "name=Seattle%2C" in url or "name=Seattle," in url:
                raise AssertionError(url)
            if "geocoding-api" in url and "Seattle" in url:
                return 200, {}, json.dumps({"results": SEATTLE}).encode()
            if "geocoding-api" in url and "Bellevue" in url:
                return 200, {}, json.dumps({"results": BELLEVUE}).encode()
            if "geocoding-api" in url and "Portland" in url:
                return 200, {}, json.dumps({"results": PORTLAND}).encode()
            if "geocoding-api" in url and "Springfield" in url:
                return 200, {}, json.dumps({"results": SPRINGFIELD}).encode()
            if "geocoding-api" in url and "%E4%B8%8A%E6%B5%B7" in url:
                return 200, {}, json.dumps({"results": SHANGHAI}).encode()
            if "api.open-meteo.com" in url:
                return 200, {}, json.dumps({"current": {"temperature_2m": 12, "weather_code": 1, "wind_speed_10m": 10}}).encode()
            return 500, {}, b""

        self.plane.skills.opener = opener
        self.plane.accounts.add_admin("owner", PASSWORD)
        status, page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": PASSWORD})
        self.assertEqual(status, 200, page)
        cookie = cookie.split(";", 1)[0]

        def send(message: str, conversation: str = "") -> str:
            body = {"csrf": page_csrf(), "message": message}
            if conversation:
                body["conversation"] = conversation
            status, body_text, _cookie = _form(self.base, "/ui/chat/send", body, cookie=cookie)
            self.assertEqual(status, 200, body_text)
            return body_text

        def page_csrf() -> str:
            _status, html = __import__("test_ui", fromlist=["_get"])._get(self.base, "/ui/chat", cookie)
            return html.split('name="csrf" value="', 1)[1].split('"', 1)[0]

        seattle = send("What's the weather in Seattle?")
        self.assertIn("Using Seattle, WA, US", seattle)
        self.assertIn("12°C", seattle)
        self.assertNotIn("Seattle Hill", seattle)
        self.assertNotIn("matches more than one", seattle)
        portland = send("What's the weather in Portland?")
        self.assertIn('class="choice"', portland)
        self.assertIn("Portland, OR", portland)
        self.assertIn("Portland, ME", portland)
        conversation = portland.split('class="conversation">', 1)[1].split("<", 1)[0]
        slot = json.loads((self.plane.accounts.home(next(item["id"] for item in self.plane.accounts.public_list() if item["username"] == "owner")) / "chat" / f"{conversation}.json").read_text())
        self.assertEqual(slot["pending_slot"]["query"], "Portland")
        geocodes = [url for url in self.seen if "geocoding-api" in url]
        chosen = send("Portland, ME", conversation)
        self.assertIn("Maine", chosen)
        self.assertIn("12°C", chosen)
        self.assertEqual([url for url in self.seen if "geocoding-api" in url], geocodes)
        before = len(self.seen)
        shanghai = send("上海的天气")
        self.assertIn("12°C", shanghai)
        self.assertIn("上海", shanghai)
        self.assertNotIn("matches more than one", shanghai)
        self.assertTrue(any("api.open-meteo.com" in url for url in self.seen[before:]))
        self.assertFalse(any("name=Seattle%2C" in url for url in self.seen))
        slot = fresh(
            "weather",
            "place",
            query="Springfield",
            choices=["Springfield, MO", "Springfield, MA"],
            candidates=[
                {"label": "Springfield, MO", "name": "Springfield", "latitude": 37.2, "longitude": -93.2, "admin1": "Missouri", "country_code": "US"},
                {"label": "Springfield, MA", "name": "Springfield", "latitude": 42.1, "longitude": -72.5, "admin1": "Massachusetts", "country_code": "US"},
            ],
            now=1_000,
        )
        picked = consume(slot, "Springfield", now=1_010)
        self.assertTrue(picked["assumed"])
        self.assertEqual(picked["candidate"]["admin1"], "Missouri")
        numbered = consume(
            fresh("weather", "place", query="Springfield", choices=["Springfield, MO", "Springfield, IL"], now=1_000),
            "1",
            now=1_010,
        )
        self.assertEqual(numbered["value"], "Springfield, MO")

    def test_freeform_place_strings_keep_the_city_and_the_region(self) -> None:
        self.assertEqual(split_place("Bellevue Washington USA"), ("Bellevue", ["Washington", "USA"]))
        self.assertEqual(split_place("Bellevue, WA"), ("Bellevue", ["WA"]))
        self.assertEqual(split_place("Bellevue, Washington"), ("Bellevue", ["Washington"]))
        self.assertEqual(split_place("Bellevue, WA, USA")[0], "Bellevue")
        self.assertEqual(split_place("上海"), ("上海", []))
        self.assertEqual(split_place("上海中国"), ("上海", ["中国"]))
        attempts = geocode_attempts("Bellevue Washington USA")
        self.assertEqual(attempts[0], ("Bellevue", "Bellevue, Washington, USA"))
        self.assertIn(("Bellevue Washington USA", "Bellevue, Washington, USA"), attempts)
        hit, ambiguous, note = _choose_place("Bellevue, Washington, USA", BELLEVUE)
        self.assertEqual(ambiguous, [])
        self.assertEqual(hit["admin1"], "Washington")
        self.assertNotIn("Nebraska", note)
        hit, ambiguous, note = _choose_place("Bellevue, WA", BELLEVUE)
        self.assertEqual(ambiguous, [])
        self.assertEqual(hit["admin1"], "Washington")
        self.assertEqual(note, "")

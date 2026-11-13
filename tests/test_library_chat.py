"""Chat finds things in Files: by a direct ask, by a bare find, and by a short follow-up."""

from __future__ import annotations

import json

from ssios.chat import iter_turn
from ssios.chat_router import classify
from ssios.library import ensure_personal, import_bytes, update_item
from ssios.library_chat import files_request, find_files, search_terms
from ssios.mcp_host import ToolSession
from ssios.orchestrator import choose_route
from support import PlaneTest
from test_library import _png

PDF = b"%PDF-1.4\n1 0 obj\nendobj\n"


class FilesInChatTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.account_id = self.account["id"]
        self.library_id = ensure_personal(self.plane, self.account_id)["id"]
        self.amc = import_bytes(self.plane, self.account_id, self.library_id, PDF + b"a", name="AMC8 2019 练习题.pdf", source="picker", peer_ip="127.0.0.1")
        self.bp = import_bytes(self.plane, self.account_id, self.library_id, PDF + b"b", name="Anthropic BP.pdf", source="picker", peer_ip="127.0.0.1")
        self.photo = import_bytes(self.plane, self.account_id, self.library_id, _png(3), name="harbor.png", source="picker", peer_ip="127.0.0.1")

    def _say(self, message: str, conversation: str = "") -> tuple[str, str, str]:
        events = list(iter_turn(self.plane, self.account, message, "auto", "127.0.0.1", conversation))
        route = next(event["data"]["route"] for event in events if event.get("event") == "route")
        conv = next(event["data"]["id"] for event in events if event.get("event") == "conversation")
        return route, json.dumps(events, ensure_ascii=False), conv

    def test_phrases_that_ask_for_a_file_and_phrases_that_do_not(self) -> None:
        for phrase in ("在我的文件里找Anthropic", "找一下AMC8的文件", "我的照片在哪", "find my Anthropic file", "search my files for AMC", "do I have a pdf about harbor", "Files里有没有BP"):
            self.assertTrue(files_request(phrase), phrase)
            self.assertEqual(classify(phrase).route, "files", phrase)
            for ready in (True, False):
                decision, note = choose_route(phrase, guest_ready=ready)
                self.assertEqual((decision.route, note), ("files", ""), phrase)
        for phrase in ("edit readme.md", "删除文件 a.txt", "write a file named notes.md", "什么是文件系统？", "check my inbox", "帮我写一个小故事", "search my drive files for the budget"):
            self.assertNotEqual(classify(phrase).route, "files", phrase)
        self.assertEqual(search_terms("在我的文件里找Anthropic"), ["anthropic"])
        self.assertEqual(search_terms("帮我找一下AMC"), ["amc"])

    def test_the_three_asks_from_the_report_list_the_file_with_a_link(self) -> None:
        route, body, conv = self._say("帮我找一下AMC")
        self.assertEqual(route, "files")
        self.assertIn("AMC8 2019 练习题.pdf", body)
        self.assertIn(f"/ui/library?library={self.library_id}&item={self.amc['id']}", body)
        self.assertNotIn("Anthropic BP", body)
        self.assertNotIn("Synthetic local answer", body)
        shown = next(event["data"] for event in json.loads(body) if event.get("event") == "results")
        self.assertEqual(shown["reply"], "")
        card = shown["blocks"][0]["events"][0]
        self.assertEqual((card["title"], card["badge"]), ("AMC8 2019 练习题.pdf", "PDF"))
        self.assertTrue(card["link"].startswith("/ui/library?library="))
        from ssios.ui_household import _public_block, _result_blocks_html

        self.assertEqual(_public_block(shown["blocks"][0])["events"][0]["link"], card["link"])
        self.assertEqual(_public_block({"events": [{"title": "x", "link": "/ui/admin"}, {"title": "y", "link": "javascript:alert(1)"}]})["events"][0]["link"], "")
        self.assertEqual(card["preview"], card["link"].replace("/ui/library?", "/ui/library/raw?"))
        self.assertEqual(_public_block(shown["blocks"][0])["events"][0]["preview"], card["preview"])
        self.assertEqual(_public_block({"events": [{"title": "x", "preview": "https://evil.example/a.pdf"}]})["events"][0]["preview"], "")
        from ssios.library_chat import files_block

        self.assertEqual(files_block([{"name": "notes.docx", "url": "/ui/library?library=l&item=i"}])["events"][0]["preview"], "")
        self.assertIn('href="/ui/library/raw?library=', _result_blocks_html(shown["blocks"]))
        self.assertIn('href="/ui/library?library=', _result_blocks_html(shown["blocks"]))
        self.assertNotIn("](", _result_blocks_html(shown["blocks"]))
        route, body, _conv = self._say("AMC8的练习题", conv)
        self.assertEqual(route, "files")
        self.assertIn("AMC8 2019 练习题.pdf", body)
        route, body, _conv = self._say("在我的文件里找Anthropic")
        self.assertEqual(route, "files")
        self.assertIn("Anthropic BP.pdf", body)
        self.assertNotIn("AMC8", body)
        stored = self.plane.accounts.home(self.account_id)
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("library.search", audit)
        self.assertNotIn("Anthropic", audit)
        del stored

    def test_no_match_says_so_or_leaves_the_turn_alone_and_trash_and_other_accounts_stay_out(self) -> None:
        route, body, _conv = self._say("在我的文件里找Zebra")
        self.assertEqual(route, "files")
        self.assertIn("Files 里没有找到匹配的文件", body)
        route, body, _conv = self._say("帮我找一下Zebra")
        self.assertNotEqual(route, "files")
        route, _body, _conv = self._say("AMC8的练习题")
        self.assertNotEqual(route, "files")
        update_item(self.plane, self.account_id, self.library_id, self.bp["id"], peer_ip="127.0.0.1", trash=True)
        self.assertEqual(find_files(self.plane, self.account_id, "Anthropic"), [])
        other = self.plane.accounts.add_member("bob", "other-horse-stable")
        self.assertEqual(find_files(self.plane, other["id"], "AMC"), [])
        self.assertEqual([row["name"] for row in find_files(self.plane, self.account_id, "找一下照片")], ["harbor.png"])

    def test_the_tools_return_ids_and_stored_text_and_count_as_outside_text(self) -> None:
        session = ToolSession(self.plane, self.account, "127.0.0.1", "find AMC")
        found = session.call("files_search", {"q": "AMC"})
        self.assertIn(self.amc["id"], found["text"])
        self.assertEqual(found["item_count"], 1)
        note = session.call("files.read", {"id": self.amc["id"]})
        self.assertIn("name: AMC8 2019 练习题.pdf", note["text"])
        self.assertNotIn("%PDF", note["text"])
        from ssios.errors import SsiosError

        with self.assertRaises(SsiosError):
            session.call("web.fetch", {"url": "https://example.com/?q=AMC8"})

    def test_tags_from_analysis_and_edits_are_listed_and_found(self) -> None:
        from ssios.library import add_tag, tag_counts
        from ssios.library_analyze import ScriptedModel, analyze_item
        from ssios.library_chat import tag_filter
        from ssios.library_page import library_page

        analyze_item(self.plane, self.account_id, self.library_id, self.amc["id"], "127.0.0.1", model=ScriptedModel("eighth grade contest problems", ["数学", "contest"], ""))
        update_item(self.plane, self.account_id, self.library_id, self.bp["id"], peer_ip="127.0.0.1", tag="work")
        update_item(self.plane, self.account_id, self.library_id, self.photo["id"], peer_ip="127.0.0.1", tag="contest")
        add_tag(self.plane, self.account_id, self.library_id, "empty", "127.0.0.1")
        self.assertEqual(dict(tag_counts(self.plane, self.account_id, self.library_id)), {"contest": 2, "数学": 1, "work": 1, "empty": 0})
        session = {"account": self.account, "csrf": "c"}
        page = library_page(self.plane, session, {"library": self.library_id})
        self.assertIn("tag=%E6%95%B0%E5%AD%A6", page)
        self.assertIn('contest <span class="count">2</span>', page)
        by_tag = library_page(self.plane, session, {"library": self.library_id, "tag": "数学"})
        self.assertIn("AMC8 2019", by_tag)
        self.assertNotIn("Anthropic BP.pdf", by_tag)
        boxed = library_page(self.plane, session, {"library": self.library_id, "q": "#contest harbor"})
        self.assertIn("harbor.png", boxed)
        self.assertNotIn("AMC8 2019", boxed)
        self.assertEqual(tag_filter("标签是数学的文件"), (["数学"], ""))
        self.assertEqual(tag_filter("files tagged work")[0], ["work"])
        for phrase, names in (("标签是数学的文件", ["AMC8 2019 练习题.pdf"]), ("show files tagged work", ["Anthropic BP.pdf"]), ("找一下 tag:contest 的图片", ["harbor.png"])):
            self.assertEqual(classify(phrase).route, "files", phrase)
            self.assertEqual([row["name"] for row in find_files(self.plane, self.account_id, phrase)], names, phrase)
        route, body, _conv = self._say("标签是数学的文件")
        self.assertEqual(route, "files")
        self.assertIn("AMC8 2019 练习题.pdf", body)


class WeatherFallbackTest(PlaneTest):
    def setUp(self) -> None:
        import ipaddress

        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.plane.skills.set_enabled(True)
        self.plane.skills.resolver = lambda host: [ipaddress.ip_address("1.1.1.1")]
        self.calls: list[str] = []

    def _open(self, url, headers=None):
        from ssios.skill_broker import classify_transport

        self.calls.append(url)
        if "open-meteo.com" in url or self.all_down:
            raise classify_transport(TimeoutError())
        if "wttr.in" in url:
            area = {"areaName": [{"value": "Seattle"}], "region": [{"value": "Washington"}], "country": [{"value": "United States of America"}], "latitude": "47.606", "longitude": "-122.332"}
            return 200, {}, json.dumps({"nearest_area": [area]}).encode()
        if "api.met.no" in url:
            step = {"instant": {"details": {"air_temperature": 9.5, "wind_speed": 5.0}}, "next_1_hours": {"summary": {"symbol_code": "partlycloudy_day"}}}
            return 200, {}, json.dumps({"properties": {"timeseries": [{"data": step}]}}).encode()
        return 500, {}, b""

    def test_when_open_meteo_is_down_the_second_source_answers_and_the_dead_host_is_skipped(self) -> None:
        self.all_down = False
        self.plane.skills.opener = self._open
        result = self.plane.skills.run(self.account["id"], "weather in Seattle", skill="weather", place="Seattle")
        card = result["blocks"][0]["events"][0]
        self.assertEqual((card["title"], card["badge"], card["when"]), ("9.5°C", "Partly cloudy", "Wind 18.0 km/h"))
        self.assertIn("Seattle", result["blocks"][0]["header"])
        self.assertEqual(sum("open-meteo.com" in url for url in self.calls), 2)
        self.assertIn("lat=47.6060&lon=-122.3320", self.calls[-1])
        before = len(self.calls)
        self.plane.skills.run(self.account["id"], "weather in Seattle", skill="weather", place="Seattle")
        self.assertFalse(any("open-meteo.com" in url for url in self.calls[before:]), self.calls[before:])

    def test_when_every_source_is_down_the_error_names_a_weather_service(self) -> None:
        from ssios.chat import failure_hint
        from ssios.errors import SsiosError
        from ssios.skill_broker import _met_no_code

        self.all_down = True
        self.plane.skills.opener = self._open
        with self.assertRaises(SsiosError) as caught:
            self.plane.skills.run(self.account["id"], "weather in Seattle", skill="weather", place="Seattle")
        self.assertEqual(caught.exception.code, "weather_unreachable")
        self.assertIn("wttr.in", caught.exception.detail)
        self.assertNotIn("clipper", failure_hint("weather_unreachable"))
        self.assertEqual([_met_no_code(name) for name in ("clearsky_night", "heavyrainshowers_day", "lightsnow", "rainandthunder", "fog", "")], [0, 80, 71, 95, 45, None])

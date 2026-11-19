"""R3 Search and filters: the filter bar, sorts in both directions, smart folders from filters, keywords."""

from __future__ import annotations

import json
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest import mock

from ssios.library import _library_root, add_folder, ensure_personal, get_item, import_bytes, metadata, update_item
from ssios.library_filter import apply, as_query, describe, parse, sort_items, sort_of
from ssios.library_page import library_page
from support import PlaneTest
from test_files_index import BLUE, RED, png, split

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def _row(n: int, **extra) -> dict:
    row = {
        "id": f"item-{n:08x}",
        "name": f"file {n}",
        "group": "image",
        "ext": "png",
        "size": 1_000_000,
        "star": 0,
        "tags": [],
        "imported_at": (NOW - timedelta(days=n)).isoformat(),
        "modified_at": (NOW - timedelta(days=n)).isoformat(),
        "width": 100,
        "height": 100,
        "orientation": "square",
        "palette": [],
    }
    row.update(extra)
    return row


class ParseTest(unittest.TestCase):
    def test_clean_values(self) -> None:
        got = parse(
            {
                "type": "image,video,spreadsheet",
                "ext": ".PNG, jpg, ../x",
                "star": "3",
                "tags": "Sky, sea，sky",
                "tagmode": "any",
                "added": "7d",
                "from": "2026-01-01",
                "to": "2026-02-30",
                "size_min": "0.5",
                "size_max": "nan",
                "shape": "portrait",
                "min_w": "1920",
                "min_h": "-4",
                "color": "#E53935",
            }
        )
        self.assertEqual(got["type"], ["image", "video"])
        self.assertEqual(got["ext"], ["png", "jpg"])
        self.assertEqual(got["star"], 3)
        self.assertEqual((got["tags"], got["tagmode"]), (["Sky", "sea"], "any"))
        self.assertEqual((got["added"], got["from"]), ("7d", "2026-01-01"))
        self.assertNotIn("to", got, "February 30 is not a date")
        self.assertEqual(got["size_min"], 0.5)
        self.assertNotIn("size_max", got)
        self.assertEqual((got["shape"], got["min_w"]), ("portrait", 1920))
        self.assertNotIn("min_h", got)
        self.assertEqual(got["color"], "e53935")

    def test_junk_is_ignored_not_an_error(self) -> None:
        self.assertEqual(parse({"star": "9", "tagmode": "some", "added": "1h", "shape": "round", "color": "red", "from": "yesterday"}), {})
        self.assertEqual(parse({"tags": "a", "tagmode": "odd"})["tagmode"], "all")

    def test_round_trip_and_labels(self) -> None:
        filters = parse({"type": "image", "tags": "a,b", "tagmode": "none", "size_min": "2", "color": "1e88e5", "star": "4"})
        self.assertEqual(parse(as_query(filters)), filters)
        labels = " | ".join(describe(filters, "en"))
        self.assertIn("Tags, none of: a, b", labels)
        self.assertIn("★ ≥ 4", labels)
        self.assertIn("2–∞ MB", labels)
        self.assertIn("颜色 #1e88e5", " ".join(describe(filters, "zh")))


class ApplyTest(unittest.TestCase):
    rows = [
        _row(0, name="sunset", tags=["sky", "sea"], star=5, size=4_000_000, width=4000, height=3000, orientation="landscape", palette=[{"color": "#e43a34", "ratio": 0.6}]),
        _row(3, name="portrait", tags=["face"], star=3, size=500_000, width=800, height=1200, orientation="portrait", palette=[{"color": "#1e86e4", "ratio": 0.5}]),
        _row(40, name="report", group="document", ext="pdf", tags=["work", "sea"], size=200_000, width=0, height=0, orientation=""),
        _row(400, name="clip", group="video", ext="mp4", tags=[], size=90_000_000, width=1920, height=1080, orientation="landscape"),
    ]

    def names(self, **query) -> list[str]:
        return [row["name"] for row in apply(self.rows, parse(query), now=NOW)]

    def test_each_filter(self) -> None:
        self.assertEqual(self.names(type="document,video"), ["report", "clip"])
        self.assertEqual(self.names(ext="pdf"), ["report"])
        self.assertEqual(self.names(star="4"), ["sunset"])
        self.assertEqual(self.names(star="3"), ["sunset", "portrait"])
        self.assertEqual(self.names(tags="sea, sky"), ["sunset"])
        self.assertEqual(self.names(tags="SEA,face", tagmode="any"), ["sunset", "portrait", "report"])
        self.assertEqual(self.names(tags="sea", tagmode="none"), ["portrait", "clip"])
        self.assertEqual(self.names(added="today"), ["sunset"])
        self.assertEqual(self.names(added="7d"), ["sunset", "portrait"])
        self.assertEqual(self.names(added="365d"), ["sunset", "portrait", "report"])
        day = (NOW - timedelta(days=40)).date().isoformat()
        self.assertEqual(self.names(**{"from": day, "to": day}), ["report"])
        self.assertEqual(self.names(size_min="1"), ["sunset", "clip"])
        self.assertEqual(self.names(size_max="0.6"), ["portrait", "report"])
        self.assertEqual(self.names(shape="landscape"), ["sunset", "clip"])
        self.assertEqual(self.names(min_w="1900"), ["sunset", "clip"])
        self.assertEqual(self.names(min_w="1000", min_h="1100"), ["sunset"])
        self.assertEqual(self.names(color="e53935"), ["sunset"])
        self.assertEqual(self.names(color="1e88e5"), ["portrait"])
        self.assertEqual(self.names(color="43a047"), [])
        self.assertEqual(self.names(type="image", shape="landscape", star="1"), ["sunset"])
        self.assertEqual(len(self.names()), 4)

    def test_sorts_both_ways(self) -> None:
        def order(**query) -> list[str]:
            key, descending = sort_of(query)
            return [row["name"] for row in sort_items(self.rows, key, descending)]

        self.assertEqual(order(), ["sunset", "portrait", "report", "clip"])
        self.assertEqual(order(sort="newest"), ["sunset", "portrait", "report", "clip"])
        self.assertEqual(order(sort="added", dir="asc"), ["clip", "report", "portrait", "sunset"])
        self.assertEqual(order(sort="name"), ["clip", "portrait", "report", "sunset"])
        self.assertEqual(order(sort="name", dir="desc"), ["sunset", "report", "portrait", "clip"])
        self.assertEqual(order(sort="size"), ["clip", "sunset", "portrait", "report"])
        self.assertEqual(order(sort="size", dir="asc"), ["report", "portrait", "sunset", "clip"])
        self.assertEqual(order(sort="star"), ["sunset", "portrait", "report", "clip"])
        self.assertEqual(order(sort="pixels"), ["sunset", "clip", "portrait", "report"])
        self.assertEqual(order(sort="modified", dir="asc"), ["clip", "report", "portrait", "sunset"])
        self.assertEqual(order(sort="bogus", dir="sideways"), ["sunset", "portrait", "report", "clip"])


class FilterPageTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.account_id = self.account["id"]
        self.library_id = ensure_personal(self.plane, self.account_id)["id"]

    def add(self, data: bytes, name: str, **extra) -> dict:
        return import_bytes(self.plane, self.account_id, self.library_id, data, name=name, source="picker", peer_ip="127.0.0.1", **extra)

    def page(self, **query) -> str:
        return library_page(self.plane, {"account": self.account, "csrf": "c"}, {"library": self.library_id, **query})

    @staticmethod
    def shown(text: str) -> set[str]:
        return {part.split('"', 1)[0] for part in text.split('class="lib-card" data-item="')[1:]}

    def test_filters_on_the_page(self) -> None:
        red = self.add(png(split(60, 20, RED, RED)), "red-wide.png")
        blue = self.add(png(split(20, 60, BLUE, BLUE)), "blue-tall.png")
        update_item(self.plane, self.account_id, self.library_id, red["id"], peer_ip="127.0.0.1", star=4)
        update_item(self.plane, self.account_id, self.library_id, blue["id"], peer_ip="127.0.0.1", tag="sky")
        self.assertEqual(self.shown(self.page(color="e53935")), {red["id"]})
        self.assertEqual(self.shown(self.page(shape="portrait")), {blue["id"]})
        self.assertEqual(self.shown(self.page(star="3")), {red["id"]})
        self.assertEqual(self.shown(self.page(tags="sky", tagmode="none")), {red["id"]})
        self.assertEqual(self.shown(self.page(type="video")), set())
        text = self.page(shape="portrait", color="1e88e5")
        self.assertIn('class="lib-chips"', text)
        self.assertIn("Portrait", text)
        self.assertIn("<summary>Filters · 2</summary>", text)
        self.assertIn('value="portrait" selected', text)
        self.assertIn('value="1e88e5" checked', text)
        self.assertIn("Save as smart folder", text)
        self.assertNotIn("chip clear", self.page())
        self.assertIn('<datalist id="lib-tag-names"><option value="sky">', self.page())

    def test_folder_view_still_narrows_and_keeps_its_place(self) -> None:
        folder = add_folder(self.plane, self.account_id, self.library_id, "Harbor", "127.0.0.1")
        folder_id = folder["id"] if isinstance(folder, dict) else folder
        inside = self.add(png(split(8, 8, RED, RED)), "in.png", folder_id=folder_id)
        self.add(png(split(8, 8, BLUE, BLUE)), "out.png")
        text = self.page(folder=folder_id)
        self.assertEqual(self.shown(text), {inside["id"]})
        self.assertIn(f'<input type="hidden" name="folder" value="{folder_id}">', text)
        self.assertEqual(self.shown(self.page(folder=folder_id, color="1e88e5")), set())

    def test_keywords_reach_link_folder_and_every_word(self) -> None:
        folder = add_folder(self.plane, self.account_id, self.library_id, "Lighthouses", "127.0.0.1")
        folder_id = folder["id"] if isinstance(folder, dict) else folder
        one = self.add(png(split(8, 8, RED, RED)), "a.png", folder_id=folder_id)
        two = self.add(png(split(8, 8, BLUE, BLUE)), "b.png")
        root = _library_root(self.plane, self.account_id, self.library_id)
        path = root / "items" / f"{two['id']}.json"
        row = json.loads(path.read_text())
        row["source_url"] = "https://pictures.example/harbour/b.png"
        row["annotation"] = "morning walk"
        path.write_text(json.dumps(row))
        self.assertEqual(self.shown(self.page(q="lighthouses")), {one["id"]})
        self.assertEqual(self.shown(self.page(q="pictures.example")), {two["id"]})
        self.assertEqual(self.shown(self.page(q="walk morning")), {two["id"]}, "words in any order")
        self.assertEqual(self.shown(self.page(q="walk evening")), set())
        self.assertNotIn("_folder_names", json.dumps(get_item(self.plane, self.account_id, self.library_id, one["id"])), "folder names are never stored")

    def test_meaning_search_keeps_the_filters(self) -> None:
        red = self.add(png(split(8, 8, RED, RED)), "a.png")
        blue = self.add(png(split(8, 8, BLUE, BLUE)), "b.png")
        with mock.patch("ssios.library_search.meaning_ids", return_value=[blue["id"], red["id"]]):
            self.assertEqual(self.shown(self.page(q="ocean")), {red["id"], blue["id"]})
            self.assertEqual(self.shown(self.page(q="ocean", color="e53935")), {red["id"]})

    def test_save_filters_as_a_smart_folder(self) -> None:
        from ssios.library_page import library_post

        red = self.add(png(split(60, 20, RED, RED)), "red.png")
        self.add(png(split(20, 60, BLUE, BLUE)), "blue.png")
        session = {"account": self.account, "csrf": "c"}
        page = library_post(self.plane, session, {"op": "smart-filters", "library": self.library_id, "name": "Wide red", "f_shape": "landscape", "f_color": "e53935", "f_bogus": "x", "q": "red"}, "127.0.0.1")
        row = next(one for one in metadata(self.plane, self.account_id, self.library_id)["smart_folders"] if one["name"] == "Wide red")
        self.assertEqual(row["filters"], {"shape": "landscape", "color": "e53935"})
        self.assertEqual(row["q"], "red")
        self.assertEqual(self.shown(page.body if hasattr(page, "body") else str(page)), {red["id"]})
        self.assertEqual(self.shown(self.page(smart=row["id"])), {red["id"]})
        self.assertIn("Wide red", self.page(smart=row["id"]))
        # A file added later is in the folder when it matches.
        later = self.add(png(split(40, 20, RED, (230, 40, 50))), "red again.png")
        self.assertEqual(self.shown(self.page(smart=row["id"])), {red["id"], later["id"]})
        # Trashed files are not.
        update_item(self.plane, self.account_id, self.library_id, later["id"], peer_ip="127.0.0.1", trash=True)
        self.assertEqual(self.shown(self.page(smart=row["id"])), {red["id"]})
        failed = library_post(self.plane, session, {"op": "smart-filters", "library": self.library_id, "name": "Nothing"}, "127.0.0.1")
        self.assertIn("Choose a filter", failed.body if hasattr(failed, "body") else str(failed))
        self.assertNotIn("lib-card", self.page(smart="smf-00000000"))

    def test_written_queries_still_work(self) -> None:
        from ssios.library_query import add_smart_folder

        red = self.add(png(split(8, 8, RED, RED)), "red.png")
        update_item(self.plane, self.account_id, self.library_id, red["id"], peer_ip="127.0.0.1", tag="warm")
        self.add(png(split(8, 8, BLUE, BLUE)), "blue.png")
        row = add_smart_folder(self.plane, self.account_id, self.library_id, "Warm", "tag:warm", "127.0.0.1")
        self.assertEqual(self.shown(self.page(smart=row["id"])), {red["id"]})

    def test_ten_thousand_items_with_filters(self) -> None:
        first = self.add(png(split(8, 8, RED, RED)), "a.png")
        root = _library_root(self.plane, self.account_id, self.library_id)
        template = json.loads((root / "items" / f"{first['id']}.json").read_text())
        for n in range(10_000):
            row = dict(template, id=f"item-{n:08x}", name=f"f{n}.png", tags=[f"t{n % 40}"], star=n % 6, size=n * 1000, description="words " * 30)
            (root / "items" / f"item-{n:08x}.json").write_text(json.dumps(row))
        self.page()
        started = time.perf_counter()
        text = self.page(tags="t3,t4", tagmode="any", star="2", color="e53935", sort="size", dir="asc", q="f1")
        took = time.perf_counter() - started
        self.assertLess(took, 0.5, f"{took:.3f}s")
        self.assertIn("lib-card", text)


class FilterRouteTest(PlaneTest):
    def test_type_boxes_ticked_together_reach_the_page(self) -> None:
        from test_ui import _form, _get

        self.plane.accounts.add_admin("owner", "correct-horse-battery")
        _status, _page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": "correct-horse-battery"})
        cookie = cookie.split(";", 1)[0]
        status, page = _get(self.base, "/ui/library", cookie)
        library_id = page.split('data-library="', 1)[1].split('"', 1)[0]
        query = urllib.parse.urlencode([("library", library_id), ("type", "image"), ("type", "audio"), ("sort", "name"), ("dir", "desc")])
        status, page = _get(self.base, "/ui/library?" + query, cookie)
        self.assertEqual(status, 200)
        self.assertIn('name="type" value="image" checked', page)
        self.assertIn('name="type" value="audio" checked', page)
        self.assertNotIn('name="type" value="video" checked', page)
        self.assertIn('<option value="name" selected>', page)
        self.assertIn('<option value="desc" selected>', page)


if __name__ == "__main__":
    unittest.main()

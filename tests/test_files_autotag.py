"""R5 Generated tags: folder auto-tags, rule tags, model tags as suggestions, and one spelling per tag."""

from __future__ import annotations

import tempfile
from pathlib import Path

from ssios.library import add_folder, ensure_personal, get_item, import_bytes, import_relative, metadata, tag_counts, update_item
from ssios.library_analyze import ScriptedModel, analyze_item
from ssios.library_autotag import canonical, review, review_all, set_folder_tags, set_rules, tag_key
from ssios.library_batch import batch_apply
from ssios.library_page import library_page, library_post
from support import PlaneTest
from test_files_index import BLUE, RED, png, split


class AutoTagTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.account_id = self.account["id"]
        self.library_id = ensure_personal(self.plane, self.account_id)["id"]
        self.session = {"account": self.account, "csrf": "c"}
        self.n = 0

    def add(self, **extra) -> dict:
        self.n += 1
        return import_bytes(self.plane, self.account_id, self.library_id, png(split(8 + self.n, 4, (self.n * 20, 0, 0), RED)), name=f"{self.n}.png", source="picker", peer_ip="127.0.0.1", **extra)

    def tags(self, item_id: str) -> list[str]:
        return get_item(self.plane, self.account_id, self.library_id, item_id)["tags"]

    def post(self, **body) -> str:
        page = library_post(self.plane, self.session, {"library": self.library_id, **body}, "127.0.0.1")
        return page.body if hasattr(page, "body") else str(page)

    def folder(self, name: str) -> str:
        row = add_folder(self.plane, self.account_id, self.library_id, name, "127.0.0.1")
        return row["id"] if isinstance(row, dict) else row

    # Folder auto-tags

    def test_folder_tags_on_import_and_on_move(self) -> None:
        trips = self.folder("Trips")
        set_folder_tags(self.plane, self.account_id, self.library_id, trips, "travel, #2026", "127.0.0.1")
        inside = self.add(folder_id=trips)
        self.assertEqual(inside["tags"], ["travel", "2026"], "the import result already carries them")
        outside = self.add()
        self.assertEqual(self.tags(outside["id"]), [])
        update_item(self.plane, self.account_id, self.library_id, outside["id"], peer_ip="127.0.0.1", folder_id=trips)
        self.assertEqual(self.tags(outside["id"]), ["travel", "2026"])
        third = self.add()
        batch_apply(self.plane, self.account_id, self.library_id, third["id"], "folder", "127.0.0.1", folder_id=trips)
        self.assertEqual(self.tags(third["id"]), ["travel", "2026"])

    def test_folder_tags_can_be_added_to_files_already_there(self) -> None:
        box = self.folder("Box")
        first = self.add(folder_id=box)
        second = self.add(folder_id=box)
        self.add()
        text = self.post(op="folder-tags", folder=box, tags="kept", apply="1")
        self.assertIn("Added to 2 files already in the folder.", text)
        self.assertEqual((self.tags(first["id"]), self.tags(second["id"])), (["kept"], ["kept"]))
        self.assertIn("Folder auto-tags: kept", library_page(self.plane, self.session, {"library": self.library_id, "folder": box}))
        self.post(op="folder-tags", folder=box, tags="")
        self.assertEqual(self.add(folder_id=box)["tags"], [])

    def test_dropped_folder_files_get_the_folder_tags_too(self) -> None:
        album = self.folder("Album")
        set_folder_tags(self.plane, self.account_id, self.library_id, album, "photos", "127.0.0.1")
        with tempfile.TemporaryDirectory() as made:
            path = Path(made) / "x.png"
            path.write_bytes(png(split(9, 9, BLUE, BLUE)))
            row = import_relative(self.plane, self.account_id, self.library_id, path, relative="x.png", source="drag", peer_ip="127.0.0.1", folder_id=album)
        self.assertEqual(self.tags(row["id"]), ["photos"])

    # Rule tags

    def test_rule_tags_off_by_default_then_type_orientation_and_folder_names(self) -> None:
        self.assertEqual(self.add()["tags"], [])
        set_rules(self.plane, self.account_id, self.library_id, {"type": True, "orientation": True}, "127.0.0.1")
        self.assertEqual(self.add()["tags"], ["image", "landscape"])
        with tempfile.TemporaryDirectory() as made:
            path = Path(made) / "y.png"
            path.write_bytes(png(split(5, 9, BLUE, BLUE)))
            row = import_relative(self.plane, self.account_id, self.library_id, path, relative="Trip/Beach/y.png", source="drag", peer_ip="127.0.0.1")
            self.assertEqual(self.tags(row["id"]), ["image", "portrait"], "folder names stay off")
            set_rules(self.plane, self.account_id, self.library_id, {"folder_names": True}, "127.0.0.1")
            path.write_bytes(png(split(6, 9, BLUE, BLUE)))
            row = import_relative(self.plane, self.account_id, self.library_id, path, relative="Trip/Beach/z.png", source="drag", peer_ip="127.0.0.1")
            self.assertEqual(self.tags(row["id"]), ["Trip", "Beach"])

    def test_rule_tags_in_chinese_and_from_the_settings_form(self) -> None:
        import json

        prefs = self.plane.accounts.home(self.account_id) / "skills.json"
        current = json.loads(prefs.read_text()) if prefs.is_file() else {"enabled": [], "location": ""}
        current["language"] = "zh"
        prefs.write_text(json.dumps(current))
        self.post(op="rule-tags", type="1", orientation="1")
        self.assertEqual(metadata(self.plane, self.account_id, self.library_id)["rule_tags"], {"type": True, "orientation": True, "folder_names": False})
        tags = self.add()["tags"]
        self.assertEqual(tags, ["图片", "横向"])
        page = library_page(self.plane, self.session, {"library": self.library_id})
        self.assertIn('name="type" value="1" checked', page)
        self.assertIn("把方向加为标签", page)
        self.assertIn('name="folder_names" value="1">', page)

    # One spelling per tag

    def test_case_and_spacing_join_the_existing_tag(self) -> None:
        first = self.add()
        update_item(self.plane, self.account_id, self.library_id, first["id"], peer_ip="127.0.0.1", tag="Sea View")
        second = self.add()
        update_item(self.plane, self.account_id, self.library_id, second["id"], peer_ip="127.0.0.1", tag="  sea   view ")
        third = self.add()
        self.post(op="tag-item", item=third["id"], tag="#SEA VIEW, new")
        fourth = self.add()
        batch_apply(self.plane, self.account_id, self.library_id, fourth["id"], "tag", "127.0.0.1", tag="sea view")
        update_item(self.plane, self.account_id, self.library_id, fourth["id"], peer_ip="127.0.0.1", tag="SEA VIEW")
        self.assertEqual(self.tags(second["id"]), ["Sea View"])
        self.assertEqual(self.tags(third["id"]), ["Sea View", "new"])
        self.assertEqual(self.tags(fourth["id"]), ["Sea View"], "one copy, not two")
        self.assertEqual(dict(tag_counts(self.plane, self.account_id, self.library_id))["Sea View"], 4)
        self.assertEqual(canonical(["a", "A ", "#a", "b/c", ""], {}), ["a"])
        self.assertEqual(tag_key("  #Sea   View "), "sea view")

    # Model tags are suggestions

    def analyze(self, item_id: str, tags: list[str]) -> dict:
        return analyze_item(self.plane, self.account_id, self.library_id, item_id, "127.0.0.1", model=ScriptedModel("a red picture", tags, ""))

    def test_model_tags_wait_for_the_owner(self) -> None:
        item = self.add()
        update_item(self.plane, self.account_id, self.library_id, item["id"], peer_ip="127.0.0.1", tag="Red")
        done = self.analyze(item["id"], ["red", "sunset", "Warm", "sunset"])
        self.assertEqual(done["tags"], ["Red"])
        self.assertEqual(done["suggested_tags"], ["sunset", "Warm"], "a suggestion the item already has is dropped")
        page = library_page(self.plane, self.session, {"library": self.library_id, "item": item["id"]})
        self.assertIn("Not added until you accept", page)
        self.assertIn('aria-label="Accept sunset"', page)
        self.assertIn("Suggested tags<span", page)
        self.post(op="suggest-accept", item=item["id"], tag="sunset")
        self.assertEqual(self.tags(item["id"]), ["Red", "sunset"])
        self.post(op="suggest-dismiss", item=item["id"], tag="Warm")
        row = get_item(self.plane, self.account_id, self.library_id, item["id"])
        self.assertEqual(row["suggested_tags"], [])
        self.assertEqual(row["dismissed_tags"], ["Warm"])
        again = self.analyze(item["id"], ["warm", "dusk"])
        self.assertEqual(again["suggested_tags"], ["dusk"], "a dismissed tag is not suggested again")
        self.post(op="suggest-accept", item=item["id"], tag="*")
        self.assertEqual(self.tags(item["id"]), ["Red", "sunset", "dusk"])
        self.assertIn("dusk", dict(tag_counts(self.plane, self.account_id, self.library_id)))

    def test_accept_or_dismiss_across_the_library(self) -> None:
        one, two, three = self.add(), self.add(), self.add()
        self.analyze(one["id"], ["a", "b"])
        self.analyze(two["id"], ["c"])
        page = library_page(self.plane, self.session, {"library": self.library_id, "suggested": "1"})
        self.assertIn("2 files have suggested tags.", page)
        self.assertEqual(page.count('class="lib-card"'), 2)
        self.post(op="suggest-all", decision="accept")
        self.assertEqual((self.tags(one["id"]), self.tags(two["id"]), self.tags(three["id"])), (["a", "b"], ["c"], []))
        self.analyze(three["id"], ["d"])
        self.assertEqual(review_all(self.plane, self.account_id, self.library_id, "127.0.0.1", accept=False), 1)
        self.assertEqual(self.tags(three["id"]), [])
        self.assertNotIn("&amp;suggested=1", library_page(self.plane, self.session, {"library": self.library_id}), "the view hides when nothing waits")

    def test_background_analysis_also_only_suggests(self) -> None:
        from ssios.background import analyze_next, set_auto_analyze

        item = self.add()
        set_auto_analyze(self.plane, self.account_id, self.library_id, True, "127.0.0.1")
        self.assertEqual(analyze_next(self.plane, {"id": self.account_id}, model=ScriptedModel("x", ["auto"], "")), item["id"])
        row = get_item(self.plane, self.account_id, self.library_id, item["id"])
        self.assertEqual((row["tags"], row["suggested_tags"]), ([], ["auto"]))

    def test_review_needs_add_rights_and_ignores_unknown_names(self) -> None:
        item = self.add()
        self.analyze(item["id"], ["x"])
        result = review(self.plane, self.account_id, self.library_id, item["id"], "127.0.0.1", accept="not offered")
        self.assertEqual(result["accepted"], [])
        self.assertEqual(self.tags(item["id"]), [])

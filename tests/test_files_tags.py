"""R4 Tags: rename, merge, and delete across a library; groups with colours; tags on one item; autocomplete."""

from __future__ import annotations

from ssios.errors import SsiosError
from ssios.library import ensure_personal, get_item, import_bytes, metadata, tag_counts, update_item
from ssios.library_page import library_page, library_post
from ssios.library_tags import add_group, colour_of, delete_tag, remove_item_tag, rename_tag, set_tag_group, update_group
from support import PlaneTest
from test_files_index import BLUE, RED, png, split


class TagManagerTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.account_id = self.account["id"]
        self.library_id = ensure_personal(self.plane, self.account_id)["id"]
        self.session = {"account": self.account, "csrf": "c"}
        self.items = []
        for n, tags in enumerate((["Sky", "sea"], ["sky"], ["sea", "harbor"], [])):
            item = import_bytes(self.plane, self.account_id, self.library_id, png(split(4, 4, (n * 50, 0, 0), RED)), name=f"{n}.png", source="picker", peer_ip="127.0.0.1")
            for tag in tags:
                update_item(self.plane, self.account_id, self.library_id, item["id"], peer_ip="127.0.0.1", tag=tag)
            self.items.append(item["id"])

    def tags_of(self, n: int) -> list[str]:
        return get_item(self.plane, self.account_id, self.library_id, self.items[n])["tags"]

    def counts(self) -> dict:
        return dict(tag_counts(self.plane, self.account_id, self.library_id))

    def post(self, **body) -> str:
        page = library_post(self.plane, self.session, {"library": self.library_id, **body}, "127.0.0.1")
        return page.body if hasattr(page, "body") else str(page)

    def test_rename_everywhere(self) -> None:
        result = rename_tag(self.plane, self.account_id, self.library_id, "harbor", "Port", "127.0.0.1")
        self.assertEqual(result, {"name": "Port", "items": 1, "merged": False})
        self.assertEqual(self.tags_of(2), ["sea", "Port"])
        self.assertNotIn("harbor", self.counts())
        self.assertEqual(self.counts()["Port"], 1)

    def test_rename_onto_an_existing_tag_merges(self) -> None:
        set_tag_group(self.plane, self.account_id, self.library_id, "sky", "", "127.0.0.1")
        result = rename_tag(self.plane, self.account_id, self.library_id, "sea", "SKY", "127.0.0.1")
        self.assertTrue(result["merged"])
        self.assertIn(result["name"], {"Sky", "sky"})
        self.assertEqual(len(self.tags_of(0)), 1, "an item with both tags ends with one")
        self.assertEqual(self.tags_of(2), [result["name"], "harbor"])
        self.assertNotIn("sea", self.counts())

    def test_case_only_rename(self) -> None:
        rename_tag(self.plane, self.account_id, self.library_id, "harbor", "Harbor", "127.0.0.1")
        self.assertEqual(self.tags_of(2), ["sea", "Harbor"])

    def test_delete_keeps_items(self) -> None:
        result = delete_tag(self.plane, self.account_id, self.library_id, "sea", "127.0.0.1")
        self.assertEqual(result["items"], 2)
        self.assertEqual(self.tags_of(2), ["harbor"])
        self.assertNotIn("sea", self.counts())
        self.assertEqual(len(self.items), len({get_item(self.plane, self.account_id, self.library_id, one)["id"] for one in self.items}))

    def test_bad_names_and_missing_tags(self) -> None:
        with self.assertRaises(SsiosError):
            rename_tag(self.plane, self.account_id, self.library_id, "harbor", "a/b", "127.0.0.1")
        with self.assertRaises(SsiosError):
            rename_tag(self.plane, self.account_id, self.library_id, "nothing", "x", "127.0.0.1")
        with self.assertRaises(SsiosError):
            delete_tag(self.plane, self.account_id, self.library_id, "  ", "127.0.0.1")

    def test_groups_with_colours(self) -> None:
        group = add_group(self.plane, self.account_id, self.library_id, "Places", "blue", "127.0.0.1")
        with self.assertRaises(SsiosError):
            add_group(self.plane, self.account_id, self.library_id, "places", "", "127.0.0.1")
        set_tag_group(self.plane, self.account_id, self.library_id, "harbor", group["id"], "127.0.0.1")
        set_tag_group(self.plane, self.account_id, self.library_id, "sea", group["id"], "127.0.0.1")
        other = add_group(self.plane, self.account_id, self.library_id, "Weather", "#00ACC1", "127.0.0.1")
        set_tag_group(self.plane, self.account_id, self.library_id, "sea", other["id"], "127.0.0.1")
        groups = {row["name"]: row for row in metadata(self.plane, self.account_id, self.library_id)["tag_groups"]}
        self.assertEqual(groups["Places"]["tags"], ["harbor"], "a tag is in one group at a time")
        self.assertEqual(groups["Weather"]["tags"], ["sea"])
        self.assertEqual(colour_of(groups["Weather"]["color"]), "#00acc1")
        self.assertEqual(colour_of("purple"), "#8e24aa")
        self.assertEqual(colour_of("url(x)"), "")
        rename_tag(self.plane, self.account_id, self.library_id, "harbor", "Port", "127.0.0.1")
        groups = {row["name"]: row for row in metadata(self.plane, self.account_id, self.library_id)["tag_groups"]}
        self.assertEqual(groups["Places"]["tags"], ["Port"], "a renamed tag stays in its group")
        page = library_page(self.plane, self.session, {"library": self.library_id})
        self.assertIn('<p class="lib-tag-group"><span class="dot" style="background:#1e88e5"></span>Places</p>', page)
        self.assertIn("Other tags", page)
        self.assertLess(page.index("Places</p>"), page.index("Other tags"))
        update_group(self.plane, self.account_id, self.library_id, group["id"], "127.0.0.1", name="Spots", colour="red")
        update_group(self.plane, self.account_id, self.library_id, other["id"], "127.0.0.1", delete=True)
        groups = {row["name"]: row for row in metadata(self.plane, self.account_id, self.library_id)["tag_groups"]}
        self.assertEqual(set(groups), {"Spots"})
        self.assertEqual(groups["Spots"]["color"], "red")
        self.assertIn("sea", self.counts(), "deleting a group keeps its tags")
        with self.assertRaises(SsiosError):
            update_group(self.plane, self.account_id, self.library_id, "../x", "127.0.0.1", delete=True)

    def test_eagle_group_colours_show(self) -> None:
        from ssios.library import replace_metadata

        data = metadata(self.plane, self.account_id, self.library_id)
        data["tag_groups"] = [{"id": "EAGLE1", "name": "Mood", "tags": ["sky"], "color": "orange"}]
        replace_metadata(self.plane, self.account_id, self.library_id, data)
        page = library_page(self.plane, self.session, {"library": self.library_id, "manage": "tags"})
        self.assertIn("background:#fb8c00", page)
        self.assertIn('<option value="orange" selected>orange</option>', page)

    def test_manager_page_and_actions(self) -> None:
        page = library_page(self.plane, self.session, {"library": self.library_id, "manage": "tags"})
        self.assertIn('class="lib-tag-manager"', page)
        self.assertNotIn('class="lib-card"', page)
        for name, count in (("sea", 2), ("harbor", 1)):
            self.assertIn(f">{name}</a></td><td class=\"num\">{count}</td>", page)
        self.assertIn("Remove the tag sea from the files that have it (2)? The files stay.", page)
        self.assertIn("Manage tags", library_page(self.plane, self.session, {"library": self.library_id}))
        found = library_page(self.plane, self.session, {"library": self.library_id, "manage": "tags", "tq": "har"})
        self.assertIn(">harbor</a>", found)
        self.assertNotIn(">sea</a></td>", found)
        text = self.post(op="tag-rename", old="sea", new="harbor")
        self.assertIn("Merged into &quot;harbor&quot; on 2 files.", text)
        self.assertIn('<datalist id="lib-tag-names">', text)
        self.assertIn('class="lib-tag-manager"', text)
        text = self.post(op="group-add", name="Places", color="green")
        group_id = next(row["id"] for row in metadata(self.plane, self.account_id, self.library_id)["tag_groups"] if row["name"] == "Places")
        self.post(op="tag-group", tag="harbor", group=group_id)
        self.post(op="group-update", group=group_id, name="Places", color="pink")
        self.assertEqual(metadata(self.plane, self.account_id, self.library_id)["tag_groups"][0]["tags"], ["harbor"])
        text = self.post(op="tag-delete", tag="harbor")
        self.assertIn("Removed the tag from 2 files.", text)
        self.post(op="group-delete", group=group_id)
        self.assertEqual(metadata(self.plane, self.account_id, self.library_id)["tag_groups"], [])

    def test_inspector_adds_several_and_removes_one(self) -> None:
        target = self.items[3]
        self.post(op="tag-item", item=target, tag="Sky, new one，#third")
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, target)["tags"], ["Sky", "new one", "third"])
        page = library_page(self.plane, self.session, {"library": self.library_id, "item": target})
        self.assertIn('name="op" value="untag-item"', page)
        self.assertIn('aria-label="Remove the tag new one"', page)
        self.assertIn('name="tag" maxlength="200" list="lib-tag-names"', page)
        self.assertIn('<datalist id="lib-tag-names">', page)
        self.assertIn('<input name="tag" maxlength="40" list="lib-tag-names" autocomplete="off">', page, "the batch dialog autocompletes too")
        self.post(op="untag-item", item=target, tag="new one")
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, target)["tags"], ["Sky", "third"])
        remove_item_tag(self.plane, self.account_id, self.library_id, target, "SKY", "127.0.0.1")
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, target)["tags"], ["third"])
        self.assertIn("rejected", self.post(op="tag-item", item=target, tag=" , "))

    def test_viewers_cannot_manage(self) -> None:
        from ssios.library_share import share_library

        self.plane.accounts.add_member("bea", "other-horse-stable2")
        bea = next(row for row in self.plane.accounts.public_list() if row["username"] == "bea")
        shared = self._shared_library()
        share_library(self.plane, self.account_id, shared, "bea", "view", "127.0.0.1")
        with self.assertRaises(SsiosError):
            rename_tag(self.plane, bea["id"], shared, "sky", "x", "127.0.0.1")
        with self.assertRaises(SsiosError):
            delete_tag(self.plane, bea["id"], shared, "sky", "127.0.0.1")
        page = library_page(self.plane, {"account": bea, "csrf": "c"}, {"library": shared, "manage": "tags"})
        self.assertNotIn('value="tag-rename"', page)

    def _shared_library(self) -> str:
        from ssios.library import create_library

        row = create_library(self.plane, self.account_id, "Family", "127.0.0.1")
        item = import_bytes(self.plane, self.account_id, row["id"], png(split(4, 4, BLUE, BLUE)), name="b.png", source="picker", peer_ip="127.0.0.1")
        update_item(self.plane, self.account_id, row["id"], item["id"], peer_ip="127.0.0.1", tag="sky")
        return row["id"]

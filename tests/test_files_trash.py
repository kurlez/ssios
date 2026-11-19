"""R6 Delete and trash: empty trash, permanent delete, automatic emptying, folder rename and delete."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ssios.errors import SsiosError
from ssios.library import _library_root, add_folder, ensure_personal, get_item, import_bytes, list_items, metadata, update_item
from ssios.library_batch import batch_apply
from ssios.library_page import library_page, library_post
from ssios.library_trash import auto_empty, delete_folder, empty_trash, expired, purge_many, remove_from_folder, rename_folder, set_retention
from support import PlaneTest
from test_files_index import BLUE, RED, png, split


class TrashTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.account_id = self.account["id"]
        self.library_id = ensure_personal(self.plane, self.account_id)["id"]
        self.root = _library_root(self.plane, self.account_id, self.library_id)
        self.session = {"account": self.account, "csrf": "c"}
        self.n = 0

    def add(self, data: bytes | None = None, name: str = "", **extra) -> dict:
        self.n += 1
        raw = data if data is not None else png(split(4 + self.n, 4, (self.n * 9 % 256, 0, 0), RED))
        return import_bytes(self.plane, self.account_id, self.library_id, raw, name=name or f"{self.n}.png", source="picker", peer_ip="127.0.0.1", **extra)

    def trash(self, item_id: str) -> None:
        update_item(self.plane, self.account_id, self.library_id, item_id, peer_ip="127.0.0.1", trash=True)

    def blob(self, item: dict):
        return self.root / "objects" / f"{item['sha256']}.{item['ext']}"

    def post(self, **body) -> str:
        page = library_post(self.plane, self.session, {"library": self.library_id, **body}, "127.0.0.1")
        return page.body if hasattr(page, "body") else str(page)

    def ids(self, **query) -> set[str]:
        return {row["id"] for row in list_items(self.plane, self.account_id, self.library_id, **query)}

    def test_empty_trash_needs_the_word_and_keeps_live_files(self) -> None:
        live = self.add()
        gone = [self.add(), self.add()]
        for item in gone:
            self.trash(item["id"])
        with self.assertRaises(SsiosError):
            empty_trash(self.plane, self.account_id, self.library_id, "yes", "127.0.0.1")
        self.assertEqual(self.ids(trash=True), {item["id"] for item in gone})
        self.assertEqual(empty_trash(self.plane, self.account_id, self.library_id, " Delete ", "127.0.0.1"), 2)
        self.assertEqual(self.ids(trash=True), set())
        self.assertEqual(self.ids(), {live["id"]})
        for item in gone:
            self.assertFalse(self.blob(item).exists())
        self.assertTrue(self.blob(live).exists())
        self.assertEqual(empty_trash(self.plane, self.account_id, self.library_id, "删除", "127.0.0.1"), 0)

    def test_empty_trash_from_the_page(self) -> None:
        item = self.add()
        self.trash(item["id"])
        page = library_page(self.plane, self.session, {"library": self.library_id, "trash": "1"})
        self.assertIn("1 in the trash. Type delete to remove them for good:", page)
        self.assertIn('value="empty-trash"', page)
        self.assertIn("Type delete to empty the trash.", self.post(op="empty-trash", confirm="no"))
        self.assertIn("Deleted 1 file for good.", self.post(op="empty-trash", confirm="delete"))
        self.assertNotIn('value="empty-trash"', library_page(self.plane, self.session, {"library": self.library_id, "trash": "1"}), "nothing to empty")

    def test_a_shared_stored_file_stays_while_used(self) -> None:
        data = png(split(9, 9, BLUE, BLUE))
        first = self.add(data, "a.png")
        self.trash(first["id"])
        # A version of a live item that points at the same bytes keeps them.
        other = self.add()
        row = get_item(self.plane, self.account_id, self.library_id, other["id"])
        row["versions"] = list(row["versions"]) + [{"sha256": first["sha256"], "size": 1, "imported_at": "2026-01-01T00:00:00+00:00"}]
        (self.root / "items" / f"{other['id']}.json").write_text(json.dumps(row))
        purge_many(self.plane, self.account_id, self.library_id, [first["id"]], "127.0.0.1")
        self.assertTrue(self.blob(first).exists(), "still used by a version of another file")
        self.trash(other["id"])
        purge_many(self.plane, self.account_id, self.library_id, [other["id"]], "127.0.0.1")
        self.assertFalse(self.blob(first).exists())
        self.assertFalse(self.blob(other).exists())

    def test_a_waiting_duplicate_keeps_its_stored_file(self) -> None:
        data = png(split(7, 7, BLUE, RED))
        first = self.add(data, "same.png")
        self.trash(first["id"])
        staging = self.root / "staging"
        staging.mkdir(exist_ok=True)
        (staging / "conf-00000000.json").write_text(json.dumps({"digest": first["sha256"]}))
        purge_many(self.plane, self.account_id, self.library_id, [first["id"]], "127.0.0.1")
        self.assertTrue(self.blob(first).exists())

    def test_permanent_delete_of_a_selection_and_previews(self) -> None:
        items = [self.add() for _ in range(3)]
        for item in items[:2]:
            self.trash(item["id"])
        thumbs = self.root / "thumbs"
        thumbs.mkdir(exist_ok=True)
        (thumbs / f"{items[0]['id']}.png").write_bytes(b"x")
        result = batch_apply(self.plane, self.account_id, self.library_id, ",".join(row["id"] for row in items), "purge", "127.0.0.1", confirm="delete")
        self.assertEqual([row["ok"] for row in result["results"]], [True, True, False], "a live file is not deleted")
        self.assertEqual(self.ids(), {items[2]["id"]})
        self.assertFalse((thumbs / f"{items[0]['id']}.png").exists())
        with self.assertRaises(SsiosError):
            batch_apply(self.plane, self.account_id, self.library_id, items[2]["id"], "purge", "127.0.0.1", confirm="")

    def test_only_the_owner_deletes_for_good(self) -> None:
        from ssios.library import create_library
        from ssios.library_share import share_library

        shared = create_library(self.plane, self.account_id, "Family", "127.0.0.1")["id"]
        item = import_bytes(self.plane, self.account_id, shared, png(split(4, 4, RED, RED)), name="x.png", source="picker", peer_ip="127.0.0.1")
        update_item(self.plane, self.account_id, shared, item["id"], peer_ip="127.0.0.1", trash=True)
        self.plane.accounts.add_member("bea", "other-horse-stable2")
        bea = next(row for row in self.plane.accounts.public_list() if row["username"] == "bea")
        share_library(self.plane, self.account_id, shared, "bea", "organize", "127.0.0.1")
        with self.assertRaises(SsiosError):
            empty_trash(self.plane, bea["id"], shared, "delete", "127.0.0.1")
        page = library_page(self.plane, {"account": bea, "csrf": "c"}, {"library": shared, "trash": "1"})
        self.assertNotIn('value="empty-trash"', page)

    def test_automatic_emptying_is_off_until_chosen(self) -> None:
        old = self.add()
        recent = self.add()
        self.trash(old["id"])
        self.trash(recent["id"])
        path = self.root / "items" / f"{old['id']}.json"
        row = json.loads(path.read_text())
        row["trashed_at"] = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0).isoformat()
        path.write_text(json.dumps(row))
        self.assertEqual(auto_empty(self.plane, {"id": self.account_id}), 0, "off by default")
        with self.assertRaises(SsiosError):
            set_retention(self.plane, self.account_id, self.library_id, 3, "127.0.0.1")
        text = self.post(op="trash-days", days="30")
        self.assertIn("longer than 30 days", text)
        self.assertEqual(metadata(self.plane, self.account_id, self.library_id)["trash_days"], 30)
        from ssios.background import tick

        tick(self.plane)
        self.assertEqual(self.ids(trash=True), {recent["id"]})
        self.assertIn('<option value="30" selected>', library_page(self.plane, self.session, {"library": self.library_id, "trash": "1"}))
        now = datetime(2026, 11, 19, tzinfo=timezone.utc)
        rows = [{"id": "a", "trashed": True, "trashed_at": "2026-10-01T00:00:00+00:00"}, {"id": "b", "trashed": True, "trashed_at": "bad"}, {"id": "c", "trashed": False, "trashed_at": "2020-01-01T00:00:00"}]
        self.assertEqual(expired(rows, 30, now), ["a"])
        self.assertEqual(expired(rows, 0, now), [])

    def test_rename_and_delete_a_folder_keeps_its_files(self) -> None:
        trips = add_folder(self.plane, self.account_id, self.library_id, "Trips", "127.0.0.1")["id"]
        beach = add_folder(self.plane, self.account_id, self.library_id, "Beach", "127.0.0.1", parent_id=trips)["id"]
        keep = add_folder(self.plane, self.account_id, self.library_id, "Keep", "127.0.0.1")["id"]
        a = self.add(folder_id=trips)
        b = self.add(folder_id=beach)
        update_item(self.plane, self.account_id, self.library_id, b["id"], peer_ip="127.0.0.1", folder_id=keep)
        rename_folder(self.plane, self.account_id, self.library_id, trips, "Travel", "127.0.0.1")
        with self.assertRaises(SsiosError):
            rename_folder(self.plane, self.account_id, self.library_id, keep, "travel", "127.0.0.1")
        with self.assertRaises(SsiosError):
            rename_folder(self.plane, self.account_id, self.library_id, keep, "a/b", "127.0.0.1")
        self.assertIn("Travel", [row["name"] for row in metadata(self.plane, self.account_id, self.library_id)["folders"]])
        result = delete_folder(self.plane, self.account_id, self.library_id, trips, "127.0.0.1")
        self.assertEqual(result, {"folders": 2, "items": 2}, "the subfolder goes with it")
        self.assertEqual([row["id"] for row in metadata(self.plane, self.account_id, self.library_id)["folders"]], [keep])
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, a["id"])["folder_ids"], [])
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, b["id"])["folder_ids"], [keep])
        self.assertEqual(self.ids(), {a["id"], b["id"]}, "no file was deleted")
        self.assertIn(a["id"], self.ids(uncategorized=True))

    def test_folder_actions_on_the_page(self) -> None:
        folder = add_folder(self.plane, self.account_id, self.library_id, "Box", "127.0.0.1")["id"]
        item = self.add(folder_id=folder)
        page = library_page(self.plane, self.session, {"library": self.library_id, "folder": folder})
        self.assertIn("Rename or delete folder", page)
        self.assertIn("Delete the folder Box and its subfolders? The files in them stay in the library.", page)
        self.post(op="folder-rename", folder=folder, name="Crate")
        inspector = library_page(self.plane, self.session, {"library": self.library_id, "item": item["id"]})
        self.assertIn("📁 Crate", inspector)
        self.assertIn('aria-label="Take it out of Crate"', inspector)
        self.post(op="unfolder-item", item=item["id"], folder=folder)
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, item["id"])["folder_ids"], [])
        update_item(self.plane, self.account_id, self.library_id, item["id"], peer_ip="127.0.0.1", folder_id=folder)
        text = self.post(op="folder-delete", folder=folder)
        self.assertIn("Folder deleted. 1 file stayed in the library.", text)
        self.assertEqual(self.ids(), {item["id"]})

    def test_remove_from_folder_and_restore(self) -> None:
        folder = add_folder(self.plane, self.account_id, self.library_id, "F", "127.0.0.1")["id"]
        item = self.add(folder_id=folder)
        remove_from_folder(self.plane, self.account_id, self.library_id, item["id"], folder, "127.0.0.1")
        self.assertEqual(self.ids(folder=folder), set())
        self.trash(item["id"])
        update_item(self.plane, self.account_id, self.library_id, item["id"], peer_ip="127.0.0.1", restore=True)
        self.assertEqual(self.ids(), {item["id"]})

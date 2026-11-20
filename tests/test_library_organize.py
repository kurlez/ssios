"""Smart folders are computed. A watch folder is local, off until enabled, and waits for a stable file."""

from __future__ import annotations

import tempfile
from pathlib import Path

from ssios.errors import SsiosError
from ssios.library import ensure_personal, get_item, import_bytes, update_item
from ssios.library_query import add_smart_folder, matches, parse_smart_query, smart_items
from ssios.library_watch import scan_watch, set_watch
from support import PlaneTest
from test_library import PNG, _png
from test_ui import _form, _get


class SmartFolderTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.library_id = ensure_personal(self.plane, self.account["id"])["id"]

    def test_a_smart_folder_filters_without_copying(self) -> None:
        red = import_bytes(self.plane, self.account["id"], self.library_id, _png(1), name="red.png", source="picker", peer_ip="127.0.0.1")
        blue = import_bytes(self.plane, self.account["id"], self.library_id, _png(2), name="blue.png", source="picker", peer_ip="127.0.0.1")
        update_item(self.plane, self.account["id"], self.library_id, red["id"], peer_ip="127.0.0.1", tag="harbor", star=4)
        saved = add_smart_folder(self.plane, self.account["id"], self.library_id, "Harbor", "tag:harbor AND star>=3", "127.0.0.1")
        found = smart_items(self.plane, self.account["id"], self.library_id, saved["id"])
        self.assertEqual([item["id"] for item in found], [red["id"]])
        self.assertEqual(get_item(self.plane, self.account["id"], self.library_id, red["id"])["folder_ids"], [])
        excluded = add_smart_folder(self.plane, self.account["id"], self.library_id, "Other", "NOT tag:harbor", "127.0.0.1")
        names = {item["name"] for item in smart_items(self.plane, self.account["id"], self.library_id, excluded["id"])}
        self.assertEqual(names, {blue["name"]})
        either = parse_smart_query("name:red OR name:blue")
        self.assertTrue(matches(red, either))
        self.assertTrue(matches(blue, either))
        with self.assertRaises(SsiosError):
            add_smart_folder(self.plane, self.account["id"], self.library_id, "Mixed", "tag:harbor AND name:red OR ext:png", "127.0.0.1")
        status, page, cookie = _form(self.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200, page)
        status, library = _get(self.base, "/ui/library", cookie.split(";", 1)[0])
        self.assertIn("Harbor", library)
        self.assertIn("Smart folder", library)
        self.assertIn("It stays off until you turn it on.", library)
        self.assertNotIn('name="enabled" value="1" checked', library)


class WatchFolderTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.library_id = ensure_personal(self.plane, self.account["id"])["id"]

    def test_watch_imports_a_stable_local_file_and_refuses_a_network_path(self) -> None:
        with self.assertRaises(SsiosError) as network:
            set_watch(self.plane, self.account["id"], self.library_id, "smb://nas/photos", True, "127.0.0.1")
        self.assertEqual(network.exception.code, "library_watch")
        with self.assertRaises(SsiosError):
            set_watch(self.plane, self.account["id"], self.library_id, "//nas/photos", True, "127.0.0.1")
        with tempfile.TemporaryDirectory() as made:
            # On macOS the temp directory is under /var, a link to /private/var. A linked path is refused, so use the real one.
            folder = str(Path(made).resolve())
            root = Path(folder)
            (root / "red.png").write_bytes(_png(4))
            (root / ".secret.png").write_bytes(PNG)
            (root / "notes.zip").write_bytes(b"PK\x03\x04notes")
            set_watch(self.plane, self.account["id"], self.library_id, folder, False, "127.0.0.1")
            idle = scan_watch(self.plane, self.account["id"], self.library_id, "127.0.0.1")
            self.assertEqual(idle["imported"], [])
            set_watch(self.plane, self.account["id"], self.library_id, folder, True, "127.0.0.1", delete_source=False)
            first = scan_watch(self.plane, self.account["id"], self.library_id, "127.0.0.1")
            self.assertEqual(first["imported"], [])
            self.assertTrue(any(row["path"] == "red.png" for row in first["waiting"]))
            second = scan_watch(self.plane, self.account["id"], self.library_id, "127.0.0.1")
            self.assertEqual([row["path"] for row in second["imported"]], ["red.png"])
            self.assertTrue((root / "red.png").is_file())
            self.assertTrue(any(row["path"] == ".secret.png" for row in second["rejected"]))
            self.assertTrue(any(row["path"] == "notes.zip" for row in second["rejected"]))
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("library.watch", audit)
        self.assertNotIn(str(folder), audit)

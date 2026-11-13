"""A library in its own folder, a disk that is not connected, a checked move, and an Eagle library copied in."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ssios.errors import SsiosError
from ssios.library import MARKER, _library_root, create_library, ensure_personal, get_item, import_bytes, item_bytes, list_items, list_libraries, metadata
from ssios.library_eagle import import_eagle
from ssios.library_storage import move_library
from support import PlaneTest
from test_library import _png

PDF = b"%PDF-1.4\n1 0 obj\nendobj\n"


class StorageTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.account_id = self.account["id"]
        made = tempfile.TemporaryDirectory()
        self.addCleanup(made.cleanup)
        self.disk = Path(made.name).resolve() / "disk"
        self.disk.mkdir()

    def test_a_library_in_its_own_folder_and_a_disk_that_goes_away(self) -> None:
        row = create_library(self.plane, self.account_id, "Photos", "127.0.0.1", str(self.disk))
        root = Path(row["root"])
        self.assertEqual(root.parent, self.disk)
        self.assertTrue(root.name.endswith(".ssioslibrary"))
        self.assertEqual(json.loads((root / MARKER).read_text())["id"], row["id"])
        item = import_bytes(self.plane, self.account_id, row["id"], _png(3), name="a.png", source="picker", peer_ip="127.0.0.1")
        self.assertTrue((root / "objects" / f"{item['sha256']}.png").is_file())
        self.assertFalse((self.plane.accounts.home(self.account_id) / "library" / row["id"]).exists())
        listed = next(one for one in list_libraries(self.plane, self.account_id) if one["id"] == row["id"])
        self.assertEqual((listed["location"], listed["offline"]), (str(root), False))
        # Another account cannot reach the folder by naming the library id.
        other = self.plane.accounts.add_member("bob", "other-horse-stable")
        self.assertNotEqual(_library_root(self.plane, other["id"], row["id"]), root)
        self.assertEqual(list_items(self.plane, other["id"], ensure_personal(self.plane, other["id"])["id"]), [])
        # Unplug the disk.
        gone = root.with_name(root.name + ".unplugged")
        root.rename(gone)
        listed = next(one for one in list_libraries(self.plane, self.account_id) if one["id"] == row["id"])
        self.assertTrue(listed["offline"])
        with self.assertRaises(SsiosError) as caught:
            list_items(self.plane, self.account_id, row["id"])
        self.assertEqual(caught.exception.code, "library_offline")
        self.assertFalse(root.exists())
        from ssios.library_chat import find_files
        from ssios.library_page import library_page

        self.assertEqual(find_files(self.plane, self.account_id, "a.png"), [])
        page = library_page(self.plane, {"account": self.account, "csrf": "c"}, {"library": row["id"]})
        self.assertIn("not connected", page)
        self.assertFalse(root.exists())
        gone.rename(root)
        self.assertEqual(len(list_items(self.plane, self.account_id, row["id"])), 1)

    def test_bad_locations_are_refused_and_only_an_admin_names_one_on_the_page(self) -> None:
        from ssios.library_page import library_post

        for where in ("smb://nas/photos", "//nas/photos", "relative/dir", str(self.state), str(self.state / "accounts"), str(self.state.parent), str(self.disk / "missing")):
            with self.assertRaises(SsiosError) as caught:
                create_library(self.plane, self.account_id, "Bad", "127.0.0.1", where)
            self.assertEqual(caught.exception.code, "library_location", where)
        # A link is followed to the real folder, and the real folder is what is checked.
        inside = self.disk.parent / "into-state"
        inside.symlink_to(self.state / "accounts")
        with self.assertRaises(SsiosError):
            create_library(self.plane, self.account_id, "Bad", "127.0.0.1", str(inside))
        self.assertEqual(list(self.disk.iterdir()), [])
        link = self.disk.parent / "My Drive 云盘"
        link.symlink_to(self.disk)
        linked = create_library(self.plane, self.account_id, "Linked", "127.0.0.1", str(link))
        self.assertEqual(Path(linked["root"]).parent, self.disk)
        import shutil

        shutil.rmtree(linked["root"])
        session = {"account": self.account, "csrf": "c"}
        refused = library_post(self.plane, session, {"op": "create-library", "name": "Mine", "location": str(self.disk)}, "127.0.0.1")
        self.assertIn("You cannot change that.", refused.body)
        self.assertNotIn("Mine", [row["name"] for row in list_libraries(self.plane, self.account_id)])
        self.assertEqual(list(self.disk.iterdir()), [])
        moved = library_post(self.plane, session, {"op": "move-library", "library": ensure_personal(self.plane, self.account_id)["id"], "location": str(self.disk)}, "127.0.0.1")
        self.assertIn("You cannot change that.", moved.body)
        self.assertEqual(list(self.disk.iterdir()), [])
        admin = self.plane.accounts.add_admin("owner", "correct-horse-battery")
        page = library_post(self.plane, {"account": admin, "csrf": "c"}, {"op": "create-library", "name": "Mine", "location": str(self.disk)}, "127.0.0.1")
        self.assertEqual(page.status, 200)
        self.assertEqual(len(list(self.disk.iterdir())), 1)

    def test_a_move_copies_checks_switches_and_sets_the_old_files_aside(self) -> None:
        library_id = ensure_personal(self.plane, self.account_id)["id"]
        item = import_bytes(self.plane, self.account_id, library_id, _png(4), name="b.png", source="picker", peer_ip="127.0.0.1")
        old = _library_root(self.plane, self.account_id, library_id)
        report = move_library(self.plane, self.account_id, library_id, str(self.disk), "127.0.0.1")
        new = Path(report["root"])
        self.assertEqual(_library_root(self.plane, self.account_id, library_id), new)
        self.assertEqual(get_item(self.plane, self.account_id, library_id, item["id"])["name"], "b.png")
        self.assertEqual(item_bytes(self.plane, self.account_id, library_id, item["id"])[0][:4], b"\x89PNG")
        aside = Path(report["old_copy"])
        self.assertTrue((aside / "items" / f"{item['id']}.json").is_file())
        self.assertEqual([entry.name for entry in old.iterdir()], [aside.name])
        other = self.plane.accounts.add_member("bob", "other-horse-stable")
        with self.assertRaises(SsiosError):
            move_library(self.plane, other["id"], library_id, str(self.disk), "127.0.0.1")
        # A shared library keeps its files on the disk. Only the member list is in the state directory.
        from ssios.library_share import share_library

        second = create_library(self.plane, self.account_id, "Family", "127.0.0.1", str(self.disk))
        shared = import_bytes(self.plane, self.account_id, second["id"], _png(5), name="c.png", source="picker", peer_ip="127.0.0.1")
        share_library(self.plane, self.account_id, second["id"], "bob", "view", "127.0.0.1")
        self.assertTrue((Path(second["root"]) / "items" / f"{shared['id']}.json").is_file())
        self.assertEqual(get_item(self.plane, other["id"], second["id"], shared["id"])["name"], "c.png")


def _eagle(root: Path) -> Path:
    """A synthetic Eagle library in the layout importers describe."""
    lib = root / "Design.library"
    (lib / "images").mkdir(parents=True)
    head = {
        "folders": [{"id": "F1", "name": "Math", "children": [{"id": "F2", "name": "AMC/8", "children": []}]}, {"id": "F3", "name": "Work", "children": []}],
        "smartFolders": [{"id": "S1", "name": "Stars", "conditions": [{"rules": [{"property": "star", "method": ">=", "value": 4}]}]}],
        "tagsGroups": [{"id": "G1", "name": "School", "tags": ["amc8", "math"], "color": "red"}],
        "applicationVersion": "4.0.0",
    }
    (lib / "metadata.json").write_text(json.dumps(head), encoding="utf-8")

    def item(eagle_id: str, name: str, ext: str, body: bytes, **extra) -> None:
        folder = lib / "images" / f"{eagle_id}.info"
        folder.mkdir()
        (folder / f"{name}.{ext}").write_bytes(body)
        (folder / f"{name}_thumbnail.png").write_bytes(_png(9))
        meta = {"id": eagle_id, "name": name, "ext": ext, "size": len(body), "btime": 1700000000000, "mtime": 1700000500000, "modificationTime": 1700001000000, "tags": [], "folders": [], "isDeleted": False}
        meta.update(extra)
        (folder / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")

    item("AAA1", "Day 18", "pdf", PDF + b"18", tags=["amc8", "math"], folders=["F2"], star=5, annotation="practice set", url="https://example.com/amc", palettes=[{"color": [1, 2, 3], "ratio": 50}])
    item("AAA2", "harbor", "png", _png(3), tags=["photo"], folders=["F3", "F1"])
    item("AAA3", "harbor copy", "png", _png(3), tags=["dup"], folders=["F1"])
    item("AAA4", "gone", "png", _png(6), isDeleted=True)
    item("AAA5", "notes", "txt", b"plain text notes")
    item("AAA6", "tool", "exe", b"MZ\x90\x00binary")
    (lib / "images" / "AAA7.info").mkdir()
    (lib / "images" / "AAA7.info" / "metadata.json").write_text("{not json", encoding="utf-8")
    return lib


class EagleImportTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.account_id = self.account["id"]
        made = tempfile.TemporaryDirectory()
        self.addCleanup(made.cleanup)
        self.base = Path(made.name).resolve()
        self.source = _eagle(self.base)
        self.before = {str(path.relative_to(self.source)): (path.stat().st_size, path.stat().st_mtime_ns) for path in self.source.rglob("*")}

    def test_an_eagle_library_is_copied_with_folders_tags_and_stars_and_is_left_untouched(self) -> None:
        disk = self.base / "disk"
        disk.mkdir()
        report = import_eagle(self.plane, self.account_id, str(self.source), "127.0.0.1", location=str(disk))
        self.assertEqual((report["imported"], report["duplicates"], report["deleted"], report["already"]), (2, 1, 1, 0))
        self.assertEqual(report["skipped"], {"kind not stored": 2, "unreadable": 1})
        self.assertEqual(report["skipped_kinds"], {"txt": 1, "exe": 1})
        self.assertEqual(report["smart_folders_not_translated"], 1)
        self.assertTrue(report["location"].startswith(str(disk)))
        library_id = report["library_id"]
        meta = metadata(self.plane, self.account_id, library_id)
        self.assertEqual(meta["name"], "Design")
        folders = {row["name"]: row for row in meta["folders"]}
        self.assertEqual(set(folders), {"Math", "AMC-8", "Work"})
        self.assertEqual(folders["AMC-8"]["parent_id"], folders["Math"]["id"])
        self.assertEqual(meta["tag_groups"][0]["tags"], ["amc8", "math"])
        self.assertEqual(meta["eagle_smart_folders"][0]["name"], "Stars")
        items = {item["name"]: item for item in list_items(self.plane, self.account_id, library_id)}
        self.assertEqual(set(items), {"Day 18.pdf", "harbor.png"})
        day = items["Day 18.pdf"]
        self.assertEqual((day["tags"], day["star"], day["annotation"], day["source_url"], day["eagle_id"]), (["amc8", "math"], 5, "practice set", "https://example.com/amc", "AAA1"))
        self.assertEqual(day["folder_ids"], [folders["AMC-8"]["id"]])
        self.assertEqual(day["imported_at"], "2023-11-14T22:13:20+00:00")
        self.assertEqual(day["eagle"]["palettes"][0]["ratio"], 50)
        harbor = items["harbor.png"]
        self.assertEqual(harbor["tags"], ["photo", "dup"])
        self.assertEqual(set(harbor["folder_ids"]), {folders["Work"]["id"], folders["Math"]["id"]})
        self.assertEqual(harbor["eagle_also"], ["AAA3"])
        self.assertEqual(item_bytes(self.plane, self.account_id, library_id, day["id"])[0], PDF + b"18")
        # The page lists it, tags and all, and chat finds it.
        from ssios.library_chat import find_files
        from ssios.library_page import library_page

        page = library_page(self.plane, {"account": self.account, "csrf": "c"}, {"library": library_id})
        self.assertIn("Day 18.pdf", page)
        self.assertIn('amc8 <span class="count">1</span>', page)
        self.assertEqual([row["name"] for row in find_files(self.plane, self.account_id, "标签是amc8的文件")], ["Day 18.pdf"])
        # The Eagle folder is exactly as it was.
        after = {str(path.relative_to(self.source)): (path.stat().st_size, path.stat().st_mtime_ns) for path in self.source.rglob("*")}
        self.assertEqual(after, self.before)
        # Running it again continues into the same library and copies nothing twice.
        again = import_eagle(self.plane, self.account_id, str(self.source), "127.0.0.1")
        self.assertEqual((again["library_id"], again["created"], again["imported"], again["already"]), (library_id, False, 0, 3))
        self.assertEqual(len(metadata(self.plane, self.account_id, library_id)["folders"]), 3)
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("library.eagle_import", audit)
        self.assertNotIn("Day 18", audit)

    def test_a_folder_that_is_not_an_eagle_library_is_refused(self) -> None:
        for where in (str(self.base), str(self.base / "missing.library"), "smb://nas/Design.library", str(self.state)):
            with self.assertRaises(SsiosError) as caught:
                import_eagle(self.plane, self.account_id, where, "127.0.0.1")
            self.assertEqual(caught.exception.code, "eagle_source", where)

    def test_the_command_prints_a_report(self) -> None:
        import contextlib
        import io

        from ssios.cli import main

        config = str(Path(__file__).resolve().parents[1] / "config" / "ssios.example.toml")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = main(["eagle-import", "--state", str(self.state), "--config", config, "--username", "ada", "--source", str(self.source)])
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertEqual(json.loads(buffer.getvalue())["imported"], 2)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(main(["library-list", "--state", str(self.state), "--config", config, "--username", "ada"]), 0)
            self.assertEqual(main(["library-list", "--state", str(self.state), "--config", config, "--username", "nobody"]), 1)
        self.assertIn('"name": "Design"', buffer.getvalue())

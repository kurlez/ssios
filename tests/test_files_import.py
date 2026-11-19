"""R1 Import: folders keep their structure, links are fetched by the host broker, dimensions and tags are recorded."""

from __future__ import annotations

import ipaddress
import json
import struct
import tempfile
from pathlib import Path

from ssios.errors import SsiosError
from ssios.library import add_folder, ensure_folder_path, ensure_personal, get_item, import_bytes, import_relative, list_items, metadata
from ssios.library_import import import_url
from ssios.library_page import accept_drop, library_page
from support import PlaneTest
from test_library import _png


def _sized_png(width: int, height: int) -> bytes:
    import zlib

    raw = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class FolderImportTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.account_id = self.account["id"]
        self.library_id = ensure_personal(self.plane, self.account_id)["id"]
        made = tempfile.TemporaryDirectory()
        self.addCleanup(made.cleanup)
        self.root = Path(made.name)

    def drop(self, relative: str, data: bytes, **extra) -> dict:
        path = self.root / relative.replace("/", "_")
        path.write_bytes(data)
        return import_relative(self.plane, self.account_id, self.library_id, path, relative=relative, source="drag", peer_ip="127.0.0.1", **extra)

    def test_a_dropped_tree_becomes_folders_and_is_reused_on_a_second_drop(self) -> None:
        self.assertEqual(self.drop("Trip/Day 1/a.png", _png(1))["status"], "saved")
        self.assertEqual(self.drop("Trip/Day 1/b.png", _png(2))["status"], "saved")
        self.assertEqual(self.drop("trip/Day 2/c.png", _png(3))["status"], "saved")
        folders = {row["name"]: row for row in metadata(self.plane, self.account_id, self.library_id)["folders"]}
        self.assertEqual(set(folders), {"Trip", "Day 1", "Day 2"})
        self.assertEqual(folders["Day 1"]["parent_id"], folders["Trip"]["id"])
        self.assertEqual(folders["Day 2"]["parent_id"], folders["Trip"]["id"])
        self.assertEqual(len(list_items(self.plane, self.account_id, self.library_id, folder=folders["Day 1"]["id"])), 2)
        # Dropped onto an existing folder, the tree goes inside it.
        box = add_folder(self.plane, self.account_id, self.library_id, "Inbox", "127.0.0.1")
        self.drop("Scans/x.png", _png(4), folder_id=box["id"])
        scans = next(row for row in metadata(self.plane, self.account_id, self.library_id)["folders"] if row["name"] == "Scans")
        self.assertEqual(scans["parent_id"], box["id"])
        self.assertEqual(ensure_folder_path(self.plane, self.account_id, self.library_id, "", ["TRIP", "day 1"], "127.0.0.1"), folders["Day 1"]["id"])

    def test_hidden_parts_depth_and_tags(self) -> None:
        self.assertEqual(self.drop("Trip/.cache/a.png", _png(5))["status"], "rejected")
        self.assertEqual(self.drop("Trip/.DS_Store", b"x")["status"], "rejected")
        deep = "/".join(f"d{n}" for n in range(14)) + "/a.png"
        self.assertEqual(self.drop(deep, _png(6))["status"], "rejected")
        with self.assertRaises(SsiosError):
            ensure_folder_path(self.plane, self.account_id, self.library_id, "", ["ok", "../x"], "127.0.0.1")
        tagged = self.drop("pier.png", _png(7), tags=["Harbor", "harbor", "trip"])
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, tagged["id"])["tags"], ["Harbor", "trip"])
        self.assertIn("Harbor", metadata(self.plane, self.account_id, self.library_id)["tags"])
        session = {"account": self.account, "csrf": "c"}
        path = self.root / "dock.png"
        path.write_bytes(_png(8))
        result = accept_drop(self.plane, session, {"library": self.library_id, "relative": "Docks/dock.png", "tags": "dock, 码头"}, [{"path": path, "name": "dock.png"}], "127.0.0.1")
        saved = result["results"][0]
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, saved["id"])["tags"], ["dock", "码头"])

    def test_dimensions_are_recorded_and_shown(self) -> None:
        saved = import_bytes(self.plane, self.account_id, self.library_id, _sized_png(30, 20), name="wide.png", source="picker", peer_ip="127.0.0.1")
        item = get_item(self.plane, self.account_id, self.library_id, saved["id"])
        self.assertEqual((item["width"], item["height"], item["orientation"]), (30, 20, "landscape"))
        page = library_page(self.plane, {"account": self.account, "csrf": "c"}, {"library": self.library_id, "item": saved["id"]})
        self.assertIn("30 × 20 · Landscape", page)
        for part in ('name="folder_files" type="file" webkitdirectory', 'id="lib-import-tags"', 'value="import-url"'):
            self.assertIn(part, page)


class LinkImportTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.account_id = self.account["id"]
        self.library_id = ensure_personal(self.plane, self.account_id)["id"]
        self.plane.skills.resolver = lambda host: [ipaddress.ip_address("10.0.0.5" if host == "intranet.example" else "93.184.216.34")]
        self.seen: list[str] = []

        def opener(url, headers=None):
            self.seen.append(url)
            if url.endswith("/page"):
                return 200, {"Content-Type": "text/html"}, b"<html>hi</html>"
            if url.endswith("/move"):
                return 302, {"Location": "https://cdn.example.com/pic"}, b""
            if url.endswith("/pic"):
                return 200, {"Content-Type": "image/png", "Content-Disposition": 'inline; filename="harbor view.png"'}, _sized_png(12, 40)
            return 404, {}, b""

        self.plane.skills.opener = opener

    def test_off_until_web_reads_are_on_then_fetched_named_and_tagged(self) -> None:
        with self.assertRaises(SsiosError) as off:
            import_url(self.plane, self.account_id, self.library_id, "https://example.com/pic", "127.0.0.1")
        self.assertEqual(off.exception.code, "library_link")
        self.assertEqual(self.seen, [])
        self.plane.skills.set_enabled(True)
        saved = import_url(self.plane, self.account_id, self.library_id, "https://example.com/move", "127.0.0.1", tags=["web"])
        item = get_item(self.plane, self.account_id, self.library_id, saved["id"])
        self.assertEqual((item["name"], item["source_kind"], item["source_url"], item["tags"]), ("harbor view.png", "link", "https://example.com/move", ["web"]))
        self.assertEqual((item["width"], item["height"], item["orientation"]), (12, 40, "portrait"))
        self.assertEqual(self.seen, ["https://example.com/move", "https://cdn.example.com/pic"])
        for bad in ("https://example.com/page", "https://intranet.example/pic", "ftp://example.com/pic", "http://127.0.0.1/pic", "javascript:alert(1)"):
            with self.assertRaises(SsiosError, msg=bad):
                import_url(self.plane, self.account_id, self.library_id, bad, "127.0.0.1")
        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("cdn.example.com", audit)
        self.assertNotIn("harbor view", audit)


class DropRouteTest(PlaneTest):
    def test_tags_and_subfolders_survive_the_streamed_upload(self) -> None:
        import urllib.request

        from test_ui import _form, _get

        self.plane.accounts.add_admin("owner", "correct-horse-battery")
        status, _page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": "correct-horse-battery"})
        cookie = cookie.split(";", 1)[0]
        status, page = _get(self.base, "/ui/library", cookie)
        csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        library_id = page.split('data-library="', 1)[1].split('"', 1)[0]
        boundary = "----ssiostest"
        parts = []
        for name, value in (("csrf", csrf), ("library", library_id), ("folder", ""), ("relative", "Trip/Day 1/a.png"), ("source", "folder"), ("tags", "trip, 2026")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + _png(9) + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(self.base + "/ui/library/drop", data=b"".join(parts), method="POST", headers={"Cookie": cookie, "Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read())["results"][0]
        self.assertEqual(result["status"], "saved", result)
        owner = next(row for row in self.plane.accounts.public_list() if row["username"] == "owner")
        item = get_item(self.plane, owner["id"], library_id, result["id"])
        self.assertEqual(item["tags"], ["trip", "2026"])
        self.assertEqual(item["source_kind"], "folder")
        names = {row["name"] for row in metadata(self.plane, owner["id"], library_id)["folders"]}
        self.assertEqual(names, {"Trip", "Day 1"})

"""Account files: type checks, dedupe, isolation, generated notes, and trash. Synthetic bytes only."""

from __future__ import annotations

import unittest
import urllib.request

from ssios.chat import iter_turn
from ssios.errors import SsiosError
from ssios.files import FileLibrary, screenshot_request


def _chunk(tag: bytes, data: bytes) -> bytes:
    import struct
    import zlib

    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _png() -> bytes:
    import struct
    import zlib

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    pixel = b"\x00\xff\x00\x00"
    return sig + ihdr + _chunk(b"IDAT", zlib.compress(pixel)) + _chunk(b"IEND", b"")


PNG = _png()


def _png_with_text() -> bytes:
    data = b"Comment\x00synthetic"
    block = _chunk(b"tEXt", data)
    pos = PNG.rfind(b"IEND") - 4
    return PNG[:pos] + block + PNG[pos:]


class FileLibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        from support import PlaneTest
        from test_ui import _form, _get

        self._form = _form
        self._get = _get
        self.case = PlaneTest()
        self.case.setUp()
        self.addCleanup(self.case._stop)
        self.addCleanup(self.case.tmp.cleanup)
        self.case.plane.accounts.add_member("ada", "other-horse-stable")
        self.case.plane.accounts.add_member("bea", "river-horse-battery")
        self.ada = next(item for item in self.case.plane.accounts.public_list() if item["username"] == "ada")
        self.bea = next(item for item in self.case.plane.accounts.public_list() if item["username"] == "bea")
        self.library = FileLibrary(self.case.plane)
        status, _page, cookie = _form(self.case.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200)
        self.cookie = cookie.split(";", 1)[0]
        status, _page, cookie = _form(self.case.base, "/ui/login", {"username": "bea", "password": "river-horse-battery"})
        self.bea_cookie = cookie.split(";", 1)[0]

    def _csrf(self, cookie: str) -> str:
        page = self._get(self.case.base, "/ui/files", cookie)[1]
        return page.split('name="csrf" value="', 1)[1].split('"', 1)[0]

    def test_rejects_active_content_and_strips_image_metadata(self) -> None:
        with self.assertRaises(SsiosError):
            self.library.add(self.ada["id"], b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", name="picture.svg")
        with self.assertRaises(SsiosError):
            self.library.add(self.ada["id"], b"<html><script>alert(1)</script></html>", name="page.html")
        saved = self.library.add(self.ada["id"], _png_with_text(), name="desk.png")
        raw, content_type, _name = self.library.bytes(self.ada["id"], saved["id"])
        self.assertEqual(content_type, "image/png")
        self.assertNotIn(b"tEXt", raw)
        self.assertNotIn(b"synthetic", raw)
        self.assertIn(b"IHDR", raw)
        jpeg = b"\xff\xd8\xff\xe1\x00\x08EXIF\x00\x00\xff\xd9"
        photo = self.library.add(self.ada["id"], jpeg, name="shot.jpg")
        raw, _kind, _name = self.library.bytes(self.ada["id"], photo["id"])
        self.assertNotIn(b"EXIF", raw)
        self.assertTrue(raw.startswith(b"\xff\xd8"))

    def test_dedupe_isolation_registry_and_trash(self) -> None:
        first = self.library.add(self.ada["id"], PNG, name="desk.png", tags=["plant"])
        second = self.library.add(self.ada["id"], PNG, name="desk-again.png")
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])
        objects = list((self.case.plane.accounts.home(self.ada["id"]) / "files" / "objects").iterdir())
        self.assertEqual(len(objects), 1)
        self.assertEqual(self.library.list(self.bea["id"]), [])
        with self.assertRaises(SsiosError):
            self.library.bytes(self.bea["id"], first["id"])
        note = self.library.add(self.ada["id"], b"# Desk report\n\nSynthetic only.\n", name="report.md", origin="generated", source="report")
        generated = self.library.list(self.ada["id"], origin="generated")
        self.assertEqual([item["id"] for item in generated], [note["id"]])
        self.library.trash(self.ada["id"], first["id"])
        self.assertEqual(self.library.list(self.ada["id"], role="image"), [])
        self.assertEqual(self.library.list(self.ada["id"], trash=True)[0]["id"], first["id"])
        self.library.restore(self.ada["id"], first["id"])
        self.assertEqual(self.library.list(self.ada["id"], role="image")[0]["name"], "desk.png")
        with self.assertRaises(SsiosError):
            self.library.purge(self.ada["id"], first["id"], "nope")
        digest = first["sha256"]
        self.library.trash(self.ada["id"], first["id"])
        self.library.purge(self.ada["id"], first["id"], "delete")
        self.assertEqual(self.library.list(self.ada["id"], trash=True), [])
        objects = self.case.plane.accounts.home(self.ada["id"]) / "files" / "objects"
        self.assertFalse(any(path.name.startswith(digest) for path in objects.iterdir()))

    def test_files_page_and_screenshot_note(self) -> None:
        saved = self.library.add(self.ada["id"], PNG, name="desk.png", thread_id="")
        request = screenshot_request("保存这张截图 标题：Desk plant 标签：plant")
        self.assertEqual(request["title"], "Desk plant")
        self.assertIn("plant", request["tags"])
        events = list(iter_turn(self.case.plane, self.ada, "保存这张截图 标题：Desk plant 标签：plant", "auto", "127.0.0.1", file_ids=[saved["id"]]))
        text = " ".join(str(event.get("data", {}).get("text") or "") for event in events)
        self.assertIn("Saved the screenshot to Notes", text)
        self.assertIn("Clippings/", text)
        page = self._get(self.case.base, "/ui/files?q=plant", self.cookie)[1]
        self.assertIn("desk-plant.md", page)
        self.assertIn("desk.png", self._get(self.case.base, "/ui/files", self.cookie)[1])
        self.assertIn("Open files", self._get(self.case.base, "/ui/chat", self.cookie)[1])
        preview = self._get(self.case.base, "/ui/files/" + saved["id"], self.cookie)[1]
        self.assertIn("/raw", preview)
        fetched = urllib.request.Request(self.case.base + "/ui/files/" + saved["id"] + "/raw", headers={"Cookie": self.cookie})
        with urllib.request.urlopen(fetched, timeout=10) as response:
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(response.headers.get("Content-Type"), "image/png")
            self.assertEqual(response.read(), PNG)
        denied = urllib.request.Request(self.case.base + "/ui/files/" + saved["id"] + "/raw", headers={"Cookie": self.bea_cookie})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(denied, timeout=10)
        self.assertEqual(caught.exception.code, 404)
        status, _page, _cookie = self._form(
            self.case.base,
            "/ui/files",
            {"csrf": self._csrf(self.cookie), "op": "trash", "id": saved["id"]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertIn("Restore", self._get(self.case.base, "/ui/files?trash=1", self.cookie)[1])

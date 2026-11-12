"""Personal library imports, versions, and account isolation. Synthetic bytes only."""

from __future__ import annotations

import struct
import zlib

from ssios.errors import SsiosError
from ssios.files import FileLibrary
from ssios.library import IMAGE_LIMIT, import_bytes
from support import PlaneTest
from test_ui import _form, _get


PASSWORD = "correct-horse-battery"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _png() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    return sig + ihdr + _chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + _chunk(b"IEND", b"")


PNG = _png()


def _png_text() -> bytes:
    block = _chunk(b"tEXt", b"Comment\x00GPSLatitude")
    pos = PNG.rfind(b"IEND") - 4
    return PNG[:pos] + block + PNG[pos:]


class LibraryTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.plane.accounts.add_admin("owner", PASSWORD)
        self.plane.accounts.add_member("ada", "other-horse-stable")
        status, page, cookie = _form(self.base, "/ui/login", {"username": "owner", "password": PASSWORD})
        self.assertEqual(status, 200, page)
        self.cookie = cookie.split(";", 1)[0]
        status, _page, ada = _form(self.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200)
        self.ada = ada.split(";", 1)[0]
        self.owner = next(item for item in self.plane.accounts.public_list() if item["username"] == "owner")

    def test_personal_library_imports_versions_and_stays_private(self) -> None:
        status, page = _get(self.base, "/ui/library", self.cookie)
        self.assertEqual(status, 200, page)
        self.assertIn("<h1>Files</h1>", page)
        self.assertIn("Personal", page)
        self.assertNotIn("Eagle", page)
        self.assertIn('class="library"', page)
        self.assertIn('class="lib-grid"', page)
        self.assertIn("No items.", page)
        self.plane.skills.set_account(self.owner["id"], language="zh")
        status, chinese = _get(self.base, "/ui/library", self.cookie)
        self.assertIn("<h1>文件</h1>", chinese)
        self.assertIn("个人", chinese)
        self.assertNotIn("Eagle", chinese)
        self.assertNotIn(">Shared<", chinese)
        self.plane.skills.set_account(self.owner["id"], language="en")

        FileLibrary(self.plane).add(self.owner["id"], PNG, name="migrated.png")
        self.plane.accounts.home(self.owner["id"]).joinpath("library").joinpath("libraries.json").unlink()
        status, migrated = _get(self.base, "/ui/library", self.cookie)
        self.assertEqual(status, 200, migrated)
        self.assertIn("migrated.png", migrated)

        csrf = migrated.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        library_id = migrated.split('name="library" value="', 1)[1].split('"', 1)[0]
        status, saved = self._file(csrf, library_id, "harbor-desk.png", _png_text(), "picker")
        self.assertEqual(status, 200, saved)
        self.assertIn("harbor-desk.png", saved)
        self.assertNotIn("GPSLatitude", saved)
        raw_item = saved.split("harbor-desk.png", 1)[0]
        item = raw_item.split("item=", 1)[-1].split('"', 1)[0].split("&", 1)[0]
        blob = self._raw(library_id, item)
        self.assertNotIn(b"GPSLatitude", blob)
        self.assertNotIn(b"tEXt", blob)

        status, again = self._file(csrf, library_id, "harbor-desk.png", PNG, "drag")
        self.assertEqual(status, 200, again)
        self.assertIn("2 version", again)
        objects = list((self.plane.accounts.home(self.owner["id"]) / "library" / library_id / "objects").glob("*"))
        self.assertGreaterEqual(len(objects), 1)
        names = {path.name.split(".", 1)[0] for path in objects}
        self.assertEqual(len(names), len(set(names)))

        status, folder, _cookie = _form(
            self.base,
            "/ui/library",
            {"csrf": csrf, "op": "folder", "library": library_id, "name": "Harbor"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, folder)
        self.assertIn("Harbor", folder)
        status, created, _cookie = _form(
            self.base,
            "/ui/library",
            {"csrf": csrf, "op": "create-library", "name": "Studio"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200, created)
        self.assertIn("Studio", created)
        self.assertNotIn("harbor-desk.png", created.split("lib-grid", 1)[-1])

        status, ada_page = self._open(self.ada, f"/ui/library/raw?library={library_id}&item={item}")
        self.assertEqual(status, 404, ada_page)
        self.assertIn("library_missing", ada_page)
        self.assertNotIn("GPSLatitude", ada_page)

        audit = (self.state / "audit" / "security.jsonl").read_text(encoding="utf-8")
        self.assertIn("library.import", audit)
        self.assertIn("library.version", audit)
        self.assertNotIn("harbor-desk.png", audit)
        self.assertNotIn("GPSLatitude", audit)

    def test_rejects_are_specific(self) -> None:
        from ssios.scope import AccountScope

        scope = AccountScope(self.plane, self.owner)
        library_id = import_ready(self.plane, self.owner["id"])
        with self.assertRaises(SsiosError) as empty:
            import_bytes(self.plane, self.owner["id"], library_id, b"", name="empty.png", source="picker", peer_ip="127.0.0.1")
        self.assertEqual(empty.exception.code, "library_empty")
        import ssios.library as library

        original = library.IMAGE_LIMIT
        library.IMAGE_LIMIT = 8
        try:
            with self.assertRaises(SsiosError) as huge:
                import_bytes(self.plane, self.owner["id"], library_id, PNG[:9], name="big.png", source="picker", peer_ip="127.0.0.1")
            self.assertEqual(huge.exception.code, "library_too_large")
            self.assertIn("8", huge.exception.detail)
        finally:
            library.IMAGE_LIMIT = original
        with self.assertRaises(SsiosError) as archive:
            import_bytes(self.plane, self.owner["id"], library_id, b"PK\x03\x04" + b"notes", name="notes.zip", source="picker", peer_ip="127.0.0.1")
        self.assertEqual(archive.exception.code, "library_rejected")
        with self.assertRaises(SsiosError) as program:
            import_bytes(self.plane, self.owner["id"], library_id, b"MZ" + b"\x00" * 16, name="tool.exe", source="picker", peer_ip="127.0.0.1")
        self.assertEqual(program.exception.code, "library_rejected")
        with self.assertRaises(SsiosError) as hidden:
            import_bytes(self.plane, self.owner["id"], library_id, PNG, name=".secret.png", source="paste", peer_ip="127.0.0.1")
        self.assertEqual(hidden.exception.code, "library_rejected")
        with self.assertRaises(SsiosError) as partial:
            import_bytes(self.plane, self.owner["id"], library_id, PNG, name="photo.png.part", source="drag", peer_ip="127.0.0.1")
        self.assertEqual(partial.exception.code, "library_rejected")
        with self.assertRaises(SsiosError) as source:
            import_bytes(self.plane, self.owner["id"], library_id, b"print(1)\n", name="tool.py", source="picker", peer_ip="127.0.0.1")
        self.assertEqual(source.exception.code, "library_type")
        heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16
        saved = import_bytes(self.plane, self.owner["id"], library_id, heic, name="iphone.heic", source="picker", peer_ip="127.0.0.1")
        self.assertEqual(saved["ext"], "heic")
        pdf = import_bytes(self.plane, self.owner["id"], library_id, b"%PDF-1.4\n1 0 obj\nendobj\n", name="sheet.pdf", source="picker", peer_ip="127.0.0.1")
        self.assertEqual(pdf["ext"], "pdf")
        from ssios.library import purge_item, update_item

        update_item(self.plane, self.owner["id"], library_id, pdf["id"], peer_ip="127.0.0.1", trash=True)
        with self.assertRaises(SsiosError):
            purge_item(self.plane, self.owner["id"], library_id, pdf["id"], "nope", "127.0.0.1")
        purge_item(self.plane, self.owner["id"], library_id, pdf["id"], "delete", "127.0.0.1")
        from ssios.library import get_item

        with self.assertRaises(SsiosError) as missing:
            get_item(self.plane, self.owner["id"], library_id, pdf["id"])
        self.assertEqual(missing.exception.code, "library_missing")
        del scope

    def _file(self, csrf: str, library_id: str, name: str, data: bytes, source: str) -> tuple[int, str]:
        import urllib.request

        boundary = "----ssioslibrary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"csrf\"\r\n\r\n{csrf}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"library\"\r\n\r\n{library_id}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"op\"\r\n\r\nimport\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"source\"\r\n\r\n{source}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            self.base + "/ui/library",
            data=body,
            headers={"Cookie": self.cookie, "Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "text/html"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def _open(self, cookie: str, path: str) -> tuple[int, str]:
        import urllib.request

        request = urllib.request.Request(self.base + path, headers={"Cookie": cookie, "Accept": "text/html"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def _raw(self, library_id: str, item: str) -> bytes:
        import urllib.request

        request = urllib.request.Request(
            self.base + f"/ui/library/raw?library={library_id}&item={item}",
            headers={"Cookie": self.cookie},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.read()


def import_ready(plane, account_id: str) -> str:
    from ssios.library import ensure_personal

    return ensure_personal(plane, account_id)["id"]

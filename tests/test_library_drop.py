"""Finder-style drops: many files, a folder, and a streamed upload."""

from __future__ import annotations

import io
import shutil
import struct
import unittest
import zlib
from pathlib import Path

from ssios.errors import SsiosError
from ssios.library import import_path, import_relative
from ssios.library_upload import stream_multipart
from support import PlaneTest


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _png(red: int = 255) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    pixel = bytes((0, red, 0, 0))
    return sig + ihdr + _chunk(b"IDAT", zlib.compress(pixel)) + _chunk(b"IEND", b"")


PNG = _png()
PNG_B = _png(10)


def _multipart(fields: dict, files: list[tuple[str, bytes]]) -> tuple[str, bytes]:
    boundary = "----dropboundary"
    chunks = []
    for key, value in fields.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    for name, data in files:
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(chunks)


class StreamUploadTest(unittest.TestCase):
    def test_parts_land_on_disk_and_a_cap_discards_the_rest(self) -> None:
        boundary, body = _multipart({"csrf": "token", "relative": "Album/day.png"}, [("day.png", PNG)])
        dest = Path(self.id().replace(".", "_"))
        # The id is long; use a temp dir from the test framework.
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            parsed = stream_multipart(io.BytesIO(body), f"multipart/form-data; boundary={boundary}", len(body), Path(folder))
            self.assertEqual(parsed["fields"]["csrf"], "token")
            self.assertEqual(parsed["fields"]["relative"], "Album/day.png")
            self.assertEqual(len(parsed["files"]), 1)
            self.assertFalse(parsed["files"][0]["too_big"])
            self.assertEqual(parsed["files"][0]["path"].read_bytes(), PNG)
        del dest


class DropImportTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        from ssios.library import ensure_personal

        self.library_id = ensure_personal(self.plane, self.account["id"])["id"]

    def test_a_folder_keeps_structure_and_skips_hidden_files(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            day = root / "Album" / "Day"
            day.mkdir(parents=True)
            (day / "harbor.png").write_bytes(PNG)
            (day / "notes.zip").write_bytes(b"PK\x03\x04notes")
            (root / "Album" / ".secret.png").write_bytes(PNG_B)
            results = []
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                results.append(
                    import_relative(
                        self.plane,
                        self.account["id"],
                        self.library_id,
                        path,
                        relative=str(path.relative_to(root)),
                        source="drag",
                        peer_ip="127.0.0.1",
                    )
                )
            self.plane.skills.set_account(self.account["id"], language="zh")
            chinese = import_relative(
                self.plane,
                self.account["id"],
                self.library_id,
                day / "notes.zip",
                relative="Album/Day/notes.zip",
                source="drag",
                peer_ip="127.0.0.1",
            )
            self.assertEqual(chinese["reason"], "不支持拖入文件夹，请拖入文件。")
            from ssios.library import add_folder, list_items, metadata

            placed = add_folder(self.plane, self.account["id"], self.library_id, "Harbor", "127.0.0.1")
            filed = import_relative(
                self.plane,
                self.account["id"],
                self.library_id,
                day / "harbor.png",
                relative="harbor.png",
                source="drag",
                peer_ip="127.0.0.1",
                folder_id=placed["id"],
            )
            self.assertEqual(filed["status"], "saved", filed)
            items = list_items(self.plane, self.account["id"], self.library_id, folder=placed["id"])
            self.assertEqual(items[0]["name"], "harbor.png")
            folders = metadata(self.plane, self.account["id"], self.library_id)["folders"]
            self.assertEqual({row["name"] for row in folders}, {"Harbor"})
        saved = [row for row in results if row["status"] == "saved"]
        rejected = [row for row in results if row["status"] == "rejected"]
        self.assertEqual(saved, [])
        self.assertTrue(any(row["code"] == "drop_directory" for row in rejected))
        self.assertTrue(any(row["path"].endswith(".secret.png") for row in rejected))

    def test_streamed_reimport_is_a_version_and_oversize_stops(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sheet.pdf"
            path.write_bytes(b"%PDF-1.4\nhello")
            first = import_path(
                self.plane, self.account["id"], self.library_id, path, name="sheet.pdf", source="drag", peer_ip="127.0.0.1"
            )
            self.assertEqual(first["result"], "saved")
            path.write_bytes(b"%PDF-1.4\nhello again")
            second = import_path(
                self.plane, self.account["id"], self.library_id, path, name="sheet.pdf", source="drag", peer_ip="127.0.0.1"
            )
            self.assertEqual(second["result"], "version")
            self.assertEqual(second["version_count"], 2)
            objects = list((self.plane.accounts.home(self.account["id"]) / "library" / self.library_id / "objects").glob("*.pdf"))
            self.assertEqual(len(objects), 2)
            import ssios.library as library

            original = library.DOCUMENT_LIMIT
            library.DOCUMENT_LIMIT = 12
            try:
                path.write_bytes(b"%PDF-1.4\n" + b"x" * 40)
                with self.assertRaises(SsiosError) as caught:
                    import_path(
                        self.plane,
                        self.account["id"],
                        self.library_id,
                        path,
                        name="huge.pdf",
                        source="drag",
                        peer_ip="127.0.0.1",
                    )
                self.assertEqual(caught.exception.code, "library_too_large")
                self.assertIn("12", caught.exception.detail)
            finally:
                library.DOCUMENT_LIMIT = original


class PlaywrightDropTest(PlaneTest):
    def test_datatransfer_drop_of_files_and_a_directory(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")
        if not shutil.which("google-chrome") and not shutil.which("chromium") and not shutil.which("chromium-browser"):
            self.skipTest("Chrome is not installed")
        from test_ui import _form

        account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.plane.skills.set_account(account["id"], language="zh")
        status, _page, cookie = _form(self.base, "/ui/login", {"username": "ada", "password": "other-horse-stable"})
        self.assertEqual(status, 200, _page)
        token = cookie.split(";", 1)[0].split("=", 1)[1]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context()
            context.add_cookies([{"name": "ssios_session", "value": token, "url": self.base}])
            page = context.new_page()
            page.goto(self.base + "/ui/library", wait_until="domcontentloaded")
            page.wait_for_selector("#library-app")
            page.evaluate(
                """([png, pngB, zip]) => {
                  const bytes = (value) => Uint8Array.from(atob(value), (ch) => ch.charCodeAt(0));
                  const fileEntry = (file) => ({
                    isFile: true, isDirectory: false, name: file.name,
                    file: (done) => done(file)
                  });
                  const dirEntry = (name, children) => ({
                    isFile: false, isDirectory: true, name: name,
                    createReader: () => {
                      let pending = children.slice();
                      return { readEntries: (done) => { const batch = pending; pending = []; done(batch); } };
                    }
                  });
                  const one = new File([bytes(png)], "one.png");
                  const two = new File([bytes(pngB)], "two.png");
                  const harbor = new File([bytes(png)], "harbor.png");
                  const notes = new File([bytes(zip)], "notes.zip");
                  const hidden = new File([bytes(png)], ".secret.png");
                  const album = dirEntry("Album", [fileEntry(harbor), fileEntry(notes), fileEntry(hidden)]);
                  const native = DataTransferItem.prototype.webkitGetAsEntry;
                  DataTransferItem.prototype.webkitGetAsEntry = function () {
                    const file = this.getAsFile && this.getAsFile();
                    if (!file) return native ? native.call(this) : null;
                    if (file.name === "Album") return album;
                    if (file.name === "one.png") return fileEntry(one);
                    if (file.name === "two.png") return fileEntry(two);
                    return fileEntry(file);
                  };
                  window.__drop = (names) => {
                    const transfer = new DataTransfer();
                    names.forEach((name) => {
                      const raw = name === "two.png" ? bytes(pngB) : name === "Album" ? new Uint8Array([123, 125]) : bytes(png);
                      transfer.items.add(new File([raw], name));
                    });
                    document.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: transfer }));
                  };
                }""",
                [_b64(PNG), _b64(PNG_B), _b64(b"PK\x03\x04notes")],
            )
            page.evaluate("() => window.__drop(['one.png', 'Album', 'two.png'])")
            page.wait_for_function(
                "() => document.querySelectorAll('#drop-log li.ok').length >= 2"
                " && (document.getElementById('drop-log').innerText || '').includes('不支持拖入文件夹')"
            )
            text = page.inner_text("#drop-log")
            self.assertIn("one.png", text)
            self.assertIn("two.png", text)
            self.assertIn("Album", text)
            self.assertIn("不支持拖入文件夹，请拖入文件。", text)
            self.assertNotIn("harbor.png", text)
            self.assertNotIn("notes.zip", text)
            browser.close()


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode("ascii")

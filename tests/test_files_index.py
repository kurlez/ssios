"""R2 Index: sizes and colours for every item, a catalog read once per page, re-index for older items."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import struct
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

from ssios import library_catalog
from ssios.library import _library_root, _write_item, ensure_personal, get_item, import_bytes, list_items, update_item
from ssios.library_index import INDEX_VERSION, index_next, reindex_library
from ssios.library_meta import read_info
from ssios.library_page import library_page
from ssios.library_palette import decode_bmp, decode_png, near, palette_of, quantize
from support import PlaneTest


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _filter(line: bytes, up: bytes, kind: int, bpp: int) -> bytes:
    out = bytearray()
    for i, value in enumerate(line):
        a = line[i - bpp] if i >= bpp else 0
        b = up[i]
        c = up[i - bpp] if i >= bpp else 0
        if kind == 0:
            pred = 0
        elif kind == 1:
            pred = a
        elif kind == 2:
            pred = b
        elif kind == 3:
            pred = (a + b) >> 1
        else:
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
        out.append((value - pred) & 0xFF)
    return bytes(out)


def png(pixels: list[list[tuple]], *, kind: int = 2, plte: bytes = b"", trns: bytes = b"") -> bytes:
    """A PNG whose rows use every filter type in turn, so the decoder's unfiltering is checked."""
    height, width = len(pixels), len(pixels[0])
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[kind]
    raw = bytearray()
    up = bytes(width * bpp)
    for y, row in enumerate(pixels):
        line = bytes(value for pixel in row for value in pixel)
        kind_of_filter = y % 5
        raw += bytes([kind_of_filter]) + _filter(line, up, kind_of_filter, bpp)
        up = line
    body = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, kind, 0, 0, 0))
    if plte:
        body += _chunk(b"PLTE", plte)
    if trns:
        body += _chunk(b"tRNS", trns)
    return b"\x89PNG\r\n\x1a\n" + body + _chunk(b"IDAT", zlib.compress(bytes(raw))) + _chunk(b"IEND", b"")


def split(width: int, height: int, left: tuple, right: tuple, cut: float = 0.5) -> list[list[tuple]]:
    edge = int(width * cut)
    return [[left if x < edge else right for x in range(width)] for y in range(height)]


def bmp(pixels: list[list[tuple]], bits: int = 24) -> bytes:
    height, width = len(pixels), len(pixels[0])
    step = bits // 8
    stride = (width * step + 3) & ~3
    body = bytearray()
    for row in reversed(pixels):
        line = bytearray()
        for r, g, b in row:
            line += bytes((b, g, r)) + (b"\xff" if bits == 32 else b"")
        body += line + bytes(stride - len(line))
    head = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, bits, 0, len(body), 2835, 2835, 0, 0)
    return head + info + bytes(body)


RED = (220, 30, 40)
BLUE = (20, 60, 200)


class PaletteTest(unittest.TestCase):
    def test_png_every_filter_and_kind(self) -> None:
        colours = quantize(decode_png(png(split(40, 30, RED, BLUE, 0.75))))
        self.assertEqual([row["color"] for row in colours], ["#dc1e28", "#143cc8"])
        self.assertAlmostEqual(colours[0]["ratio"], 0.75, delta=0.03)
        grey = quantize(decode_png(png([[(90,)] * 8] * 8, kind=0)))
        self.assertEqual(grey[0]["color"], "#5a5a5a")
        # Transparent pixels are not a colour of the picture.
        rgba = [[(255, 0, 0, 255) if x < 4 else (0, 0, 255, 0) for x in range(16)] for _ in range(6)]
        self.assertEqual([row["color"] for row in quantize(decode_png(png(rgba, kind=6)))], ["#ff0000"])
        grey_alpha = [[(200, 255) if x < 2 else (10, 0) for x in range(8)] for _ in range(4)]
        self.assertEqual(quantize(decode_png(png(grey_alpha, kind=4)))[0]["color"], "#c8c8c8")
        indexed = png([[(0,), (1,), (1,), (1,)]] * 4, kind=3, plte=bytes(RED + BLUE))
        found = quantize(decode_png(indexed))
        self.assertEqual(found[0]["color"], "#143cc8")
        self.assertAlmostEqual(found[0]["ratio"], 0.75, delta=0.01)

    def test_png_it_cannot_read_gives_nothing(self) -> None:
        self.assertEqual(decode_png(b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 16, 2, 0, 0, 0))), [])
        self.assertEqual(decode_png(b"\x89PNG\r\n\x1a\nbroken"), [])
        self.assertEqual(decode_bmp(b"BMnot a bitmap"), [])
        self.assertEqual(quantize([]), [])

    def test_bmp_both_depths(self) -> None:
        for bits in (24, 32):
            colours = quantize(decode_bmp(bmp(split(30, 10, BLUE, RED, 0.6), bits)))
            self.assertEqual([row["color"] for row in colours], ["#143cc8", "#dc1e28"], bits)

    def test_similar_colours_join_and_at_most_five(self) -> None:
        samples = [(100, 100, 100)] * 50 + [(104, 101, 99)] * 50
        self.assertEqual(len(quantize(samples)), 1)
        rainbow = [(r, g, b) for r in (0, 120, 250) for g in (0, 120, 250) for b in (0, 250)] * 3
        self.assertEqual(len(quantize(rainbow)), 5)

    def test_near(self) -> None:
        colours = [{"color": "#dc1e28", "ratio": 0.7}, {"color": "#143cc8", "ratio": 0.02}]
        self.assertTrue(near(colours, "#ff0000"))
        self.assertTrue(near(colours, "e0202a"))
        self.assertFalse(near(colours, "#0000ff"), "a colour on 2% of the picture does not count")
        self.assertFalse(near(colours, "#00ff00"))
        self.assertFalse(near(colours, "not a colour"))

    def test_without_a_tool_png_still_works(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            import tempfile

            with tempfile.TemporaryDirectory() as made:
                path = Path(made) / "x.png"
                path.write_bytes(png(split(20, 20, RED, BLUE)))
                self.assertEqual(len(palette_of(path, "png")), 2)
                jpeg = Path(made) / "x.jpg"
                jpeg.write_bytes(b"\xff\xd8\xff\xe0" + bytes(100))
                self.assertEqual(palette_of(jpeg, "jpg"), [])
                preview = Path(made) / "page.png"
                preview.write_bytes(png(split(10, 10, BLUE, BLUE)))
                self.assertEqual(palette_of(Path(made) / "doc.pdf", "pdf", preview)[0]["color"], "#143cc8")

    @unittest.skipUnless(shutil.which("convert") or shutil.which("magick") or shutil.which("sips"), "no image tool on this computer")
    def test_jpeg_through_a_local_tool(self) -> None:
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as made:
            source = Path(made) / "x.png"
            source.write_bytes(png(split(80, 40, RED, BLUE, 0.7)))
            target = Path(made) / "x.jpg"
            tool = shutil.which("magick") or shutil.which("convert")
            if tool:
                subprocess.run([tool, str(source), "-quality", "95", str(target)], check=True, capture_output=True)
            else:
                subprocess.run(["sips", "-s", "format", "jpeg", str(source), "--out", str(target)], check=True, capture_output=True)
            colours = palette_of(target, "jpg")
            self.assertTrue(near(colours, "#dc1e28", within=20), colours)
            self.assertTrue(near(colours, "#143cc8", within=20), colours)
            self.assertGreater(colours[0]["ratio"], 0.55)
            self.assertEqual(read_info(target.read_bytes(), "jpg")["orientation"], "landscape")


def _box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + kind + body


class HeaderTest(unittest.TestCase):
    """Width and height from the header of each image format, and duration from a movie."""

    def test_formats(self) -> None:
        jpeg = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + bytes(14) + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 300, 400) + bytes(10)
        gif = b"GIF89a" + struct.pack("<HH", 64, 32) + bytes(10)
        vp8x = b"RIFF" + bytes(4) + b"WEBPVP8X" + bytes(8) + (799).to_bytes(3, "little") + (599).to_bytes(3, "little")
        bits = (1023) | (511 << 14)
        vp8l = b"RIFF" + bytes(4) + b"WEBPVP8L" + bytes(5) + bits.to_bytes(4, "little") + bytes(8)
        vp8 = b"RIFF" + bytes(4) + b"WEBPVP8 " + bytes(10) + struct.pack("<HH", 640, 640) + bytes(4)
        tiff_le = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 2) + struct.pack("<HHII", 256, 3, 1, 1200) + struct.pack("<HHII", 257, 4, 1, 900) + bytes(4)
        tiff_be = b"MM\x00*" + struct.pack(">I", 8) + struct.pack(">H", 2) + struct.pack(">HHI", 256, 3, 1) + struct.pack(">HH", 50, 0) + struct.pack(">HHII", 257, 4, 1, 70)
        heic = _box(b"ftyp", b"heic" + bytes(4)) + _box(b"meta", bytes(4) + _box(b"ispe", bytes(4) + struct.pack(">II", 4032, 3024)))
        mvhd = _box(b"mvhd", bytes(12) + struct.pack(">II", 1000, 95_500) + bytes(80))
        tkhd = _box(b"tkhd", bytes(76) + struct.pack(">II", 1920 << 16, 1080 << 16))
        movie = _box(b"ftyp", b"isom" + bytes(4)) + _box(b"moov", mvhd + _box(b"trak", tkhd))
        cases = {
            "png": (png(split(7, 3, RED, RED)), (7, 3, "landscape")),
            "jpg": (jpeg, (400, 300, "landscape")),
            "gif": (gif, (64, 32, "landscape")),
            "webp-x": (vp8x, (800, 600, "landscape")),
            "webp-l": (vp8l, (1024, 512, "landscape")),
            "webp": (vp8, (640, 640, "square")),
            "bmp": (bmp(split(5, 9, RED, RED)), (5, 9, "portrait")),
            "tif-le": (tiff_le, (1200, 900, "landscape")),
            "tif-be": (tiff_be, (50, 70, "portrait")),
            "heic": (heic, (4032, 3024, "landscape")),
            "mp4": (movie, (1920, 1080, "landscape")),
        }
        for name, (data, want) in cases.items():
            info = read_info(data, name)
            self.assertEqual((info.get("width"), info.get("height"), info.get("orientation")), want, name)
        self.assertEqual(read_info(movie, "mp4")["duration"], 95.5)
        self.assertEqual(read_info(b"%PDF-1.7", "pdf"), {})
        self.assertEqual(read_info(b"\xff\xd8\xff", "jpg"), {})


class IndexTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.account_id = self.account["id"]
        self.library_id = ensure_personal(self.plane, self.account_id)["id"]
        self.root = _library_root(self.plane, self.account_id, self.library_id)

    def add(self, data: bytes, name: str) -> dict:
        return import_bytes(self.plane, self.account_id, self.library_id, data, name=name, source="picker", peer_ip="127.0.0.1")

    def page(self, **query) -> str:
        return library_page(self.plane, {"account": self.account, "csrf": "c"}, {"library": self.library_id, **query})

    def test_import_records_size_and_colours(self) -> None:
        item = self.add(png(split(60, 20, RED, BLUE, 0.8)), "flag.png")
        self.assertEqual((item["width"], item["height"], item["orientation"]), (60, 20, "landscape"))
        self.assertEqual(item["indexed"], INDEX_VERSION)
        self.assertEqual(item["palette"][0]["color"], "#dc1e28")
        text = self.page(item=item["id"])
        self.assertIn('class="lib-palette"', text)
        self.assertIn("background:#dc1e28", text)
        self.assertIn("&amp;color=dc1e28", text)

    def test_same_name_different_content_is_indexed_on_its_own(self) -> None:
        item = self.add(png(split(10, 10, RED, RED)), "a.png")
        other = self.add(png(split(10, 30, BLUE, BLUE)), "a.png")
        self.assertEqual(other["result"], "saved", other)
        self.assertNotEqual(other["id"], item["id"])
        self.assertEqual((other["orientation"], other["palette"][0]["color"]), ("portrait", "#143cc8"))
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, item["id"])["orientation"], "square")

    def strip(self, item_id: str) -> None:
        row = get_item(self.plane, self.account_id, self.library_id, item_id)
        for key in ("width", "height", "orientation", "palette", "indexed"):
            row.pop(key, None)
        _write_item(self.plane, self.account_id, self.library_id, row)

    def test_reindex_fills_older_items_and_keeps_edits(self) -> None:
        first = self.add(png(split(30, 60, BLUE, BLUE)), "tall.png")
        second = self.add(png(split(30, 30, RED, RED)), "square.png")
        self.strip(first["id"])
        self.strip(second["id"])
        update_item(self.plane, self.account_id, self.library_id, first["id"], peer_ip="127.0.0.1", tag="sky")
        report = reindex_library(self.plane, self.account_id, self.library_id)
        self.assertEqual(report["indexed"], 2)
        row = get_item(self.plane, self.account_id, self.library_id, first["id"])
        self.assertEqual((row["orientation"], row["palette"][0]["color"], row["tags"]), ("portrait", "#143cc8", ["sky"]))
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, second["id"])["orientation"], "square")
        self.assertEqual(reindex_library(self.plane, self.account_id, self.library_id)["indexed"], 0, "nothing left to do")
        self.assertEqual(reindex_library(self.plane, self.account_id, self.library_id, force=True)["indexed"], 2)

    def test_reindex_stops_at_its_time_budget(self) -> None:
        for n in range(3):
            self.strip(self.add(png(split(8, 8, (n * 60, 0, 0), RED)), f"{n}.png")["id"])
        report = reindex_library(self.plane, self.account_id, self.library_id, budget=0.0)
        self.assertEqual(report["indexed"], 0)
        self.assertEqual(report["left"], 3)

    def test_background_pass_and_command_line(self) -> None:
        item = self.add(png(split(12, 6, RED, RED)), "wide.png")
        self.strip(item["id"])
        self.assertEqual(index_next(self.plane, {"id": self.account_id}), 1)
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, item["id"])["orientation"], "landscape")
        self.strip(item["id"])
        from ssios.background import tick

        tick(self.plane)
        self.assertEqual(get_item(self.plane, self.account_id, self.library_id, item["id"])["indexed"], INDEX_VERSION)
        self.strip(item["id"])
        from ssios.cli import main

        config = str(Path(__file__).resolve().parents[1] / "config" / "ssios.example.toml")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["library-reindex", "--state", str(self.state), "--config", config, "--username", "ada"])
        self.assertEqual(code, 0, buffer.getvalue())
        self.assertEqual(json.loads(buffer.getvalue())["libraries"][self.library_id]["indexed"], 1)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(main(["library-reindex", "--state", str(self.state), "--config", config, "--username", "ada", "--library", "lib-nope"]), 1)

    def test_page_shows_more_link_past_page_size(self) -> None:
        for n in range(5):
            self.add(png(split(4, 4, (n * 40, 10, 10), RED)), f"{n}.png")
        with mock.patch("ssios.library_page.PAGE_SIZE", 2):
            text = self.page()
            self.assertEqual(text.count('class="lib-card"'), 2)
            self.assertIn("limit=4", text)
            self.assertIn("2 / 5" if "已显示" in text else "Showing 2 of 5", text)
            self.assertEqual(self.page(limit="4").count('class="lib-card"'), 4)
            self.assertNotIn("lib-more", self.page(limit="99"))
            self.assertEqual(self.page(limit="nonsense").count('class="lib-card"'), 2)


class CatalogTest(PlaneTest):
    def setUp(self) -> None:
        super().setUp()
        self.account = self.plane.accounts.add_member("ada", "other-horse-stable")
        self.account_id = self.account["id"]
        self.library_id = ensure_personal(self.plane, self.account_id)["id"]
        self.root = _library_root(self.plane, self.account_id, self.library_id)
        self.first = import_bytes(self.plane, self.account_id, self.library_id, png(split(4, 4, RED, RED)), name="a.png", source="picker", peer_ip="127.0.0.1")

    def rows(self) -> list[dict]:
        return list_items(self.plane, self.account_id, self.library_id)

    def test_unchanged_files_are_not_parsed_again(self) -> None:
        self.rows()
        before = library_catalog.stats()["parsed"]
        self.rows()
        self.rows()
        self.assertEqual(library_catalog.stats()["parsed"], before)

    def test_restart_reads_the_cache_file_and_not_every_item(self) -> None:
        for n in range(4):
            import_bytes(self.plane, self.account_id, self.library_id, png(split(4, 4, (n, 9, 9), RED)), name=f"{n}.png", source="picker", peer_ip="127.0.0.1")
        self.rows()
        self.assertTrue((self.root / library_catalog.CACHE).is_file())
        library_catalog.forget()
        stats = library_catalog.stats()
        self.assertEqual(len(self.rows()), 5)
        after = library_catalog.stats()
        self.assertEqual(after["parsed"], stats["parsed"], "a restart reads one cache file, not five item files")
        self.assertEqual(after["disk_loads"], stats["disk_loads"] + 1)

    def test_a_file_changed_on_disk_is_read_again(self) -> None:
        self.rows()
        library_catalog.forget()
        path = self.root / "items" / f"{self.first['id']}.json"
        row = json.loads(path.read_text())
        row["annotation"] = "changed by a sync tool"
        path.write_text(json.dumps(row))
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 5_000_000))
        self.assertEqual(self.rows()[0]["annotation"], "changed by a sync tool")
        # A new file appears and one goes away.
        copy = dict(row, id="item-0000aaaa", name="b.png")
        (self.root / "items" / "item-0000aaaa.json").write_text(json.dumps(copy))
        self.assertEqual({one["id"] for one in self.rows()}, {self.first["id"], "item-0000aaaa"})
        path.unlink()
        self.assertEqual([one["id"] for one in self.rows()], ["item-0000aaaa"])

    def test_broken_cache_symlinks_and_other_files_are_ignored(self) -> None:
        self.rows()
        library_catalog.forget()
        (self.root / library_catalog.CACHE).write_text("{not json")
        self.assertEqual(len(self.rows()), 1)
        library_catalog.forget()
        (self.root / library_catalog.CACHE).write_text(json.dumps({"version": 1, "rows": {"item-x.json": [1, 2, 3, {"id": "item-x"}]}}))
        self.assertEqual([one["id"] for one in self.rows()], [self.first["id"]], "a row whose file is gone is dropped")
        (self.root / "items" / "notes.txt").write_text("x")
        os.symlink(self.root / "items" / f"{self.first['id']}.json", self.root / "items" / "item-11112222.json")
        (self.root / "items" / "item-33334444.json").write_text("{broken")
        self.assertEqual([one["id"] for one in self.rows()], [self.first["id"]])

    def test_changing_a_returned_row_does_not_change_the_catalog(self) -> None:
        row = self.rows()[0]
        row["tags"].append("leak")
        row["name"] = "leak"
        row["versions"][0]["size"] = -1
        again = self.rows()[0]
        self.assertEqual((again["tags"], again["name"]), ([], "a.png"))
        self.assertNotEqual(again["versions"][0]["size"], -1)

    def test_page_reads_the_catalog_once(self) -> None:
        calls = []
        real = library_catalog.rows

        def counted(root):
            calls.append(root)
            return real(root)

        with mock.patch.object(library_catalog, "rows", counted):
            text = library_page(self.plane, {"account": self.account, "csrf": "c"}, {"library": self.library_id})
        self.assertEqual(len(calls), 1)
        self.assertIn('class="lib-card"', text)

    def test_ten_thousand_items(self) -> None:
        template = json.loads((self.root / "items" / f"{self.first['id']}.json").read_text())
        folder = self.root / "items"
        for n in range(10_000):
            row = dict(template, id=f"item-{n:08x}", name=f"f{n}.png", tags=[f"t{n % 40}"], description="words " * 30)
            (folder / f"item-{n:08x}.json").write_text(json.dumps(row))
        self.rows()
        started = time.perf_counter()
        self.assertEqual(len(self.rows()), 10_001)
        listing = time.perf_counter() - started
        started = time.perf_counter()
        text = library_page(self.plane, {"account": self.account, "csrf": "c"}, {"library": self.library_id, "tag": "t3"})
        page = time.perf_counter() - started
        self.assertIn("t3", text)
        self.assertLess(listing, 0.5, f"listing took {listing:.3f}s")
        self.assertLess(page, 0.5, f"page took {page:.3f}s")
        library_catalog.forget()
        started = time.perf_counter()
        self.rows()
        self.assertLess(time.perf_counter() - started, 1.5, "after a restart the cache file is read instead of 10,000 files")


if __name__ == "__main__":
    unittest.main()

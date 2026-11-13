"""Preview images for Files. A PDF becomes a first-page image when a local tool can draw it."""

from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import time
import zlib
from pathlib import Path

RENDER_SECONDS = 6.0

_GLYPH = {
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
}


def png_size(data: bytes) -> tuple[int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    return (160, 200)


def pdf_tile() -> bytes:
    """A small page icon. It is a real PNG, not a broken image."""
    width, height = 160, 200
    scale = 6
    letters = "PDF"
    glyph_w = 5 * scale + scale
    block_w = glyph_w * len(letters) - scale
    origin_x = (width - block_w) // 2
    origin_y = (height - 7 * scale) // 2
    rows = []
    for y in range(height):
        pixel = bytearray()
        for x in range(width):
            edge = x < 8 or y < 8 or x >= width - 8 or y >= height - 8
            fold = x > width - 36 and y < 36
            ink = _ink(x, y, origin_x, origin_y, scale, glyph_w)
            if ink:
                pixel += b"\x1f\x29\x3d"
            elif fold:
                pixel += b"\xc5\xcd\xd6"
            elif edge:
                pixel += b"\xd5\xda\xe3"
            else:
                pixel += b"\xf7\xf8\xfb"
        rows.append(b"\x00" + bytes(pixel))
    return _png(width, height, b"".join(rows))


def preview_png(plane, account_id: str, library_id: str, item: dict) -> bytes:
    """An image the browser can show. PDFs use a cached first page, or the tile."""
    from ssios.library import _library_root, item_bytes

    ext = str(item.get("ext") or "")
    if ext in {"png", "jpg", "gif", "webp", "bmp"}:
        data, _mime = item_bytes(plane, account_id, library_id, item["id"])
        return data
    cache = _library_root(plane, account_id, library_id) / "thumbs" / f"{item['id']}.png"
    if ext == "pdf":
        if cache.is_file() and not cache.is_symlink() and cache.stat().st_size > 32:
            return cache.read_bytes()
        # A page that could not be drawn is not tried again for this version of the file.
        failed = cache.with_name(f"{item['id']}.{str(item.get('sha256') or '')[:16]}.failed")
        if not failed.exists():
            raw, _mime = item_bytes(plane, account_id, library_id, item["id"])
            drawn = _render_pdf(raw)
            cache.parent.mkdir(parents=True, exist_ok=True)
            if drawn:
                cache.write_bytes(drawn)
                cache.chmod(0o600)
                return drawn
            failed.write_bytes(b"")
            failed.chmod(0o600)
    return pdf_tile() if ext == "pdf" else _label_tile(ext.upper() or "FILE")


def _render_pdf(raw: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="ssios-pdf-") as folder:
        src = Path(folder) / "page.pdf"
        src.write_bytes(raw)
        out = Path(folder) / "page.png"
        if _run_renderer(src, out):
            data = out.read_bytes()
            if data.startswith(b"\x89PNG"):
                return data
    return b""


def _run_renderer(src: Path, out: Path) -> bool:
    stem = str(out.with_suffix(""))
    commands = []
    if shutil.which("pdftoppm"):
        commands.append(["pdftoppm", "-png", "-f", "1", "-l", "1", "-scale-to", "480", "-singlefile", str(src), stem])
    if shutil.which("pdftocairo"):
        commands.append(["pdftocairo", "-png", "-f", "1", "-l", "1", "-scale-to", "480", "-singlefile", str(src), stem])
    if shutil.which("qlmanage"):
        commands.append(["qlmanage", "-t", "-s", "480", "-o", str(out.parent), str(src)])
    if shutil.which("sips"):
        commands.append(["sips", "-s", "format", "png", str(src), "--out", str(out)])
    # The browser is waiting for this image. All tools together get RENDER_SECONDS.
    deadline = time.monotonic() + RENDER_SECONDS
    for command in commands:
        left = deadline - time.monotonic()
        if left <= 0.2:
            break
        try:
            subprocess.run(command, check=False, timeout=left, capture_output=True)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.is_file():
            return True
        numbered = Path(stem + "-1.png")
        if numbered.is_file():
            numbered.replace(out)
            return True
        quick = out.parent / (src.name + ".png")
        if quick.is_file():
            quick.replace(out)
            return True
    return False


def _ink(x: int, y: int, origin_x: int, origin_y: int, scale: int, glyph_w: int) -> bool:
    local_x = x - origin_x
    local_y = y - origin_y
    if local_x < 0 or local_y < 0:
        return False
    index = local_x // glyph_w
    if index < 0 or index > 2:
        return False
    glyph = _GLYPH["PDF"[index]]
    col = (local_x % glyph_w) // scale
    row = local_y // scale
    if row >= len(glyph) or col >= len(glyph[0]):
        return False
    return glyph[row][col] == "1"


def _label_tile(label: str) -> bytes:
    return pdf_tile() if label == "PDF" else _plain_tile()


def _plain_tile() -> bytes:
    from ssios.library import solid_png

    return solid_png(160, 200, (230, 233, 239))


def _png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

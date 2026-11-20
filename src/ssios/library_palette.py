"""Main colours of an image, as Eagle shows them, with no image library.

A local tool makes a small BMP when one is installed (``sips`` on a Mac, ImageMagick, or ffmpeg), which covers
JPEG, HEIC, GIF, WebP, TIFF, and the first frame of a video. Without one, PNG and BMP files are decoded here in
Python, and a PDF uses its cached first-page preview. The result is at most five colours, each with the share of
the picture it covers. Nothing leaves the computer.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

TOOL_SECONDS = 10.0
SIDE = 64
# Pure-Python PNG decoding is slow; bigger files wait for a tool.
_PY_PIXELS = 1_500_000
_IMAGES = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif", "avif"}
_MOVIES = {"mp4", "mov", "m4v", "webm", "mkv"}


def palette_of(path: Path, ext: str, preview: Path | None = None) -> list[dict]:
    """Up to five colours as [{"color": "#rrggbb", "ratio": 0.42}], largest first. Empty when nothing can be read."""
    ext = str(ext or "").casefold()
    samples: list[tuple[int, int, int]] = []
    if ext in _IMAGES or ext in _MOVIES:
        samples = _with_tool(path, movie=ext in _MOVIES)
        if not samples and ext in {"png", "bmp"}:
            samples = _decode_file(path)
    if not samples and preview is not None and preview.is_file() and not preview.is_symlink():
        samples = _decode_file(preview)
    return quantize(samples)


def _decode_file(path: Path) -> list[tuple[int, int, int]]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return decode_png(data)
    if data.startswith(b"BM"):
        return decode_bmp(data)
    return []


def _with_tool(path: Path, *, movie: bool) -> list[tuple[int, int, int]]:
    with tempfile.TemporaryDirectory(prefix="ssios-palette-") as folder:
        out = Path(folder) / "small.bmp"
        commands = []
        if movie:
            if shutil.which("ffmpeg"):
                commands.append(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", "1", "-i", str(path), "-frames:v", "1", "-vf", f"scale={SIDE}:-2", "-pix_fmt", "bgr24", str(out)])
                commands.append(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(path), "-frames:v", "1", "-vf", f"scale={SIDE}:-2", "-pix_fmt", "bgr24", str(out)])
        else:
            if shutil.which("sips"):
                commands.append(["sips", "-s", "format", "bmp", "-Z", str(SIDE), str(path), "--out", str(out)])
            for name in ("magick", "convert"):
                if shutil.which(name):
                    commands.append([name, f"{path}[0]", "-auto-orient", "-thumbnail", f"{SIDE}x{SIDE}", "-background", "white", "-flatten", "-type", "TrueColor", f"BMP3:{out}"])
                    break
            if shutil.which("ffmpeg"):
                commands.append(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(path), "-frames:v", "1", "-vf", f"scale={SIDE}:-2", "-pix_fmt", "bgr24", str(out)])
        for command in commands:
            try:
                subprocess.run(command, check=False, timeout=TOOL_SECONDS, capture_output=True, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if out.is_file():
                found = decode_bmp(out.read_bytes())
                if found:
                    return found
    return []


def decode_bmp(data: bytes) -> list[tuple[int, int, int]]:
    """Uncompressed 24-bit and 32-bit BMP, every few pixels."""
    try:
        if not data.startswith(b"BM") or len(data) < 54:
            return []
        offset = struct.unpack("<I", data[10:14])[0]
        width, height = struct.unpack("<ii", data[18:26])
        bits = struct.unpack("<H", data[28:30])[0]
        compression = struct.unpack("<I", data[30:34])[0]
    except struct.error:
        return []
    if bits not in (24, 32) or compression not in (0, 3) or width <= 0 or height == 0:
        return []
    rows = abs(height)
    step = bits // 8
    stride = (width * step + 3) & ~3
    every = max(1, int((width * rows / 4096) ** 0.5))
    out = []
    for y in range(0, rows, every):
        start = offset + y * stride
        line = data[start : start + width * step]
        if len(line) < width * step:
            break
        for x in range(0, width, every):
            at = x * step
            out.append((line[at + 2], line[at + 1], line[at]))
    return out


def decode_png(data: bytes) -> list[tuple[int, int, int]]:
    """8-bit grey, RGB, palette, grey with alpha, and RGBA PNGs that are not interlaced. Transparent pixels are left out."""
    try:
        width, height, depth, kind, _c, _f, interlace = struct.unpack(">IIBBBBB", data[16:29])
    except struct.error:
        return []
    if depth != 8 or interlace or kind not in (0, 2, 3, 4, 6) or width * height > _PY_PIXELS or not width or not height:
        return []
    chunks = []
    plte = b""
    trns = b""
    at = 8
    while at + 8 <= len(data):
        size, name = struct.unpack(">I4s", data[at : at + 8])
        body = data[at + 8 : at + 8 + size]
        if name == b"IDAT":
            chunks.append(body)
        elif name == b"PLTE":
            plte = body
        elif name == b"tRNS":
            trns = body
        elif name == b"IEND":
            break
        at += 12 + size
    try:
        raw = zlib.decompress(b"".join(chunks))
    except zlib.error:
        return []
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[kind]
    stride = width * channels
    if len(raw) < (stride + 1) * height:
        return []
    every = max(1, int((width * height / 4096) ** 0.5))
    previous = bytearray(stride)
    out = []
    for y in range(height):
        start = y * (stride + 1)
        kind_of_filter = raw[start]
        line = bytearray(raw[start + 1 : start + 1 + stride])
        _unfilter(line, previous, kind_of_filter, channels)
        previous = line
        if y % every:
            continue
        for x in range(0, width, every):
            at = x * channels
            if kind == 2:
                out.append((line[at], line[at + 1], line[at + 2]))
            elif kind == 6:
                if line[at + 3] >= 128:
                    out.append((line[at], line[at + 1], line[at + 2]))
            elif kind == 0:
                out.append((line[at],) * 3)
            elif kind == 4:
                if line[at + 1] >= 128:
                    out.append((line[at],) * 3)
            else:
                index = line[at]
                if index * 3 + 3 <= len(plte) and (index >= len(trns) or trns[index] >= 128):
                    out.append((plte[index * 3], plte[index * 3 + 1], plte[index * 3 + 2]))
    return out


def _unfilter(line: bytearray, up: bytearray, kind: int, bpp: int) -> None:
    if kind == 0:
        return
    size = len(line)
    if kind == 1:
        for i in range(bpp, size):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif kind == 2:
        for i in range(size):
            line[i] = (line[i] + up[i]) & 0xFF
    elif kind == 3:
        for i in range(size):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + up[i]) >> 1)) & 0xFF
    elif kind == 4:
        for i in range(size):
            a = line[i - bpp] if i >= bpp else 0
            b = up[i]
            c = up[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF


def quantize(samples: list[tuple[int, int, int]], count: int = 5) -> list[dict]:
    """Group pixels into 4-bit-per-channel bins, join bins that look alike, and keep the largest groups."""
    if not samples:
        return []
    bins: dict[int, list[int]] = {}
    for r, g, b in samples:
        key = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)
        slot = bins.get(key)
        if slot is None:
            bins[key] = [1, r, g, b]
        else:
            slot[0] += 1
            slot[1] += r
            slot[2] += g
            slot[3] += b
    groups: list[list[float]] = []
    for n, r, g, b in sorted(bins.values(), key=lambda row: -row[0]):
        colour = (r / n, g / n, b / n)
        for group in groups:
            if _distance(group[1:], colour) < 34:
                total = group[0] + n
                group[1:] = [(group[1 + i] * group[0] + colour[i] * n) / total for i in range(3)]
                group[0] = total
                break
        else:
            groups.append([n, *colour])
    total = len(samples)
    groups.sort(key=lambda row: -row[0])
    out = []
    for n, r, g, b in groups[:count]:
        ratio = round(n / total, 3)
        if ratio < 0.01 and out:
            break
        out.append({"color": "#%02x%02x%02x" % (round(r), round(g), round(b)), "ratio": ratio})
    return out


def _distance(one, two) -> float:
    # Weighted RGB distance, close enough to how far apart two colours look.
    dr, dg, db = one[0] - two[0], one[1] - two[1], one[2] - two[2]
    mean = (one[0] + two[0]) / 2
    return ((2 + mean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - mean) / 256) * db * db) ** 0.5 / 3


def hex_rgb(value: str) -> tuple[int, int, int] | None:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def near(palette: list, target: str, *, within: float = 60.0, min_ratio: float = 0.05) -> bool:
    """True when one of the item's main colours is close to ``target``. The colour filter in R3 uses this."""
    want = hex_rgb(target)
    if want is None:
        return False
    for row in palette or []:
        have = hex_rgb(row.get("color") if isinstance(row, dict) else "")
        if have is not None and float(row.get("ratio") or 0) >= min_ratio and _distance(have, want) <= within:
            return True
    return False

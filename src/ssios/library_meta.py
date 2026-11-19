"""Width, height, orientation, and duration of a stored file, read from its header. No image library is needed.

Images: PNG, JPEG, GIF, WebP, BMP, TIFF, HEIC, and AVIF. Video and audio in MP4 and MOV containers: size of
the first video track and the duration. Anything else, or a header that cannot be read, gives nothing.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

_HEAD = 4_000_000
_TAIL = 4_000_000


def _orientation(width: int, height: int) -> str:
    if not width or not height:
        return ""
    if abs(width - height) <= max(width, height) * 0.02:
        return "square"
    return "landscape" if width > height else "portrait"


def _jpeg(data: bytes) -> tuple[int, int]:
    at = 2
    while at + 9 < len(data):
        if data[at] != 0xFF:
            at += 1
            continue
        marker = data[at + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            at += 2
            continue
        if marker == 0xFF:
            at += 1
            continue
        length = struct.unpack(">H", data[at + 2 : at + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[at + 5 : at + 9])
            return width, height
        at += 2 + length
    return 0, 0


def _webp(data: bytes) -> tuple[int, int]:
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if kind == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    if kind == b"VP8 " and len(data) >= 30:
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    return 0, 0


def _tiff(data: bytes) -> tuple[int, int]:
    order = "<" if data[:2] == b"II" else ">"
    try:
        offset = struct.unpack(order + "I", data[4:8])[0]
        count = struct.unpack(order + "H", data[offset : offset + 2])[0]
        found = {}
        for index in range(min(count, 200)):
            entry = data[offset + 2 + index * 12 : offset + 14 + index * 12]
            tag, kind = struct.unpack(order + "HH", entry[:4])
            if tag in (256, 257):
                value = struct.unpack(order + ("H" if kind == 3 else "I"), entry[8:10] if kind == 3 else entry[8:12])[0]
                found[tag] = value
        return found.get(256, 0), found.get(257, 0)
    except (struct.error, IndexError):
        return 0, 0


def _boxes(data: bytes, start: int, end: int):
    at = start
    while at + 8 <= end:
        size, kind = struct.unpack(">I4s", data[at : at + 8])
        head = 8
        if size == 1 and at + 16 <= end:
            size = struct.unpack(">Q", data[at + 8 : at + 16])[0]
            head = 16
        elif size == 0:
            size = end - at
        if size < head:
            return
        yield kind, at + head, min(at + size, end)
        at += size


def _heif(data: bytes) -> dict:
    """HEIC and AVIF store the image size in an ispe box."""
    at = data.find(b"ispe", 0, 400_000)
    if at < 0:
        return {}
    width, height = struct.unpack(">II", data[at + 8 : at + 16])
    return {"width": width, "height": height}


def _movie(data: bytes, start: int = 0) -> dict:
    """MP4 and MOV: duration from mvhd, size from the first track header that has one."""
    info: dict = {}
    stack = [(start, len(data))]
    while stack:
        begin, end = stack.pop()
        for kind, body, stop in _boxes(data, begin, end):
            if kind in (b"moov", b"trak", b"mdia", b"minf"):
                stack.append((body, stop))
            elif kind == b"mvhd" and "duration" not in info and stop - body >= 20:
                version = data[body]
                if version == 1:
                    scale, length = struct.unpack(">IQ", data[body + 20 : body + 32])
                else:
                    scale, length = struct.unpack(">II", data[body + 12 : body + 20])
                if scale:
                    info["duration"] = round(length / scale, 2)
            elif kind == b"tkhd" and "width" not in info and stop - body >= 84:
                width, height = struct.unpack(">II", data[stop - 8 : stop])
                if width >> 16 and height >> 16:
                    info.update(width=width >> 16, height=height >> 16)
    return info


def read_info(data: bytes, ext: str) -> dict:
    ext = str(ext or "").casefold()
    width = height = 0
    extra: dict = {}
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            width, height = struct.unpack(">II", data[16:24])
        elif data.startswith(b"\xff\xd8"):
            width, height = _jpeg(data)
        elif data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
            width, height = struct.unpack("<HH", data[6:10])
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            width, height = _webp(data)
        elif data.startswith(b"BM") and len(data) >= 26:
            width, height = struct.unpack("<ii", data[18:26])
            height = abs(height)
        elif data.startswith((b"II*\x00", b"MM\x00*")):
            width, height = _tiff(data)
        elif len(data) > 12 and data[4:8] == b"ftyp":
            heif = data[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"avif")
            extra = _heif(data) if heif else _movie(data)
            width, height = int(extra.pop("width", 0) or 0), int(extra.pop("height", 0) or 0)
    except (struct.error, IndexError, ValueError):
        return {}
    info: dict = {}
    if 0 < width < 100_000 and 0 < height < 100_000:
        info.update(width=int(width), height=int(height), orientation=_orientation(width, height))
    if extra.get("duration") and 0 < float(extra["duration"]) < 10 * 86400:
        info["duration"] = float(extra["duration"])
    del ext
    return info


def file_info(path, ext: str = "") -> dict:
    """Read the head of the file, and for a movie whose index is written at the end, its tail too."""
    target = Path(path)
    try:
        size = os.path.getsize(target)
        with open(target, "rb") as handle:
            head = handle.read(_HEAD)
            info = read_info(head, ext)
            movie = len(head) > 12 and head[4:8] == b"ftyp" and head[8:12] not in (b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"avif")
            if movie and size > _HEAD and ("duration" not in info or "width" not in info):
                handle.seek(max(_HEAD, size - _TAIL))
                tail = handle.read(_TAIL)
                at = tail.find(b"moov")
                if at >= 4:
                    more = _movie(tail, at - 4)
                    if "width" not in info and more.get("width") and more.get("height"):
                        info.update(width=more["width"], height=more["height"], orientation=_orientation(more["width"], more["height"]))
                    if "duration" not in info and more.get("duration"):
                        info["duration"] = float(more["duration"])
            return info
    except OSError:
        return {}


def describe(info: dict) -> str:
    parts = []
    if info.get("width") and info.get("height"):
        parts.append(f"{info['width']} × {info['height']}")
    if info.get("duration"):
        seconds = int(round(float(info["duration"])))
        parts.append(f"{seconds // 60}:{seconds % 60:02d}")
    return " · ".join(parts)

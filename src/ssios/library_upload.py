"""Stream a Files drop to disk. The request body is not kept as one buffer."""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from ssios.errors import SsiosError
from ssios.library import MEDIA_LIMIT

_CHUNK = 64 * 1024


class _Reader:
    def __init__(self, source, length: int) -> None:
        self.source = source
        self.left = max(0, length)
        self.buf = b""

    def _pull(self) -> None:
        if not self.left:
            return
        chunk = self.source.read(min(_CHUNK, self.left))
        if not chunk:
            self.left = 0
            return
        self.left -= len(chunk)
        self.buf += chunk

    def read_until(self, marker: bytes, sink=None, limit: int | None = None) -> tuple[bytes, bool]:
        """Return text before marker. A sink receives file bytes in chunks and stops at limit."""
        written = 0
        too_big = False
        collected = bytearray()
        keep = max(0, len(marker) - 1)
        while True:
            found = self.buf.find(marker)
            if found >= 0:
                piece = self.buf[:found]
                self.buf = self.buf[found + len(marker) :]
                written, too_big = self._emit(piece, sink, collected, written, limit, too_big)
                return bytes(collected), too_big
            if len(self.buf) > keep:
                piece = self.buf[:-keep] if keep else self.buf
                self.buf = self.buf[-keep:] if keep else b""
                written, too_big = self._emit(piece, sink, collected, written, limit, too_big)
            if not self.left and marker not in self.buf:
                raise SsiosError("bad_request", "upload rejected", 400)
            self._pull()

    def _emit(self, piece: bytes, sink, collected: bytearray, written: int, limit: int | None, too_big: bool) -> tuple[int, bool]:
        if not piece:
            return written, too_big
        if sink is None:
            collected.extend(piece)
            return written, too_big
        if too_big:
            return written, True
        if limit is not None and written + len(piece) > limit:
            room = max(0, limit - written)
            if room:
                sink.write(piece[:room])
            return written + room, True
        sink.write(piece)
        return written + len(piece), False


def stream_multipart(source, content_type: str, length: int, dest: Path) -> dict:
    """Write each file part to dest. Text fields stay short. Oversized parts are discarded."""
    match = re.search(r"boundary=([^;]+)", content_type or "")
    if match is None:
        raise SsiosError("bad_request", "upload rejected", 400)
    marker = match.group(1).strip().strip('"')
    if not marker or len(marker) > 200:
        raise SsiosError("bad_request", "upload rejected", 400)
    try:
        token = marker.encode("ascii")
    except UnicodeError as exc:
        raise SsiosError("bad_request", "upload rejected", 400) from exc
    reader = _Reader(source, length)
    reader.read_until(b"--" + token)
    fields: dict[str, str] = {}
    files: list[dict] = []
    while True:
        peek = reader.buf[:2]
        while len(peek) < 2 and reader.left:
            reader._pull()
            peek = reader.buf[:2]
        if peek.startswith(b"--"):
            break
        if reader.buf.startswith(b"\r\n"):
            reader.buf = reader.buf[2:]
        header, _ = reader.read_until(b"\r\n\r\n")
        found = re.search(br'name="([^"]+)"', header)
        if found is None:
            reader.read_until(b"\r\n--" + token)
            continue
        name = found.group(1).decode("ascii", "replace")
        named = re.search(br'filename="([^"]*)"', header)
        if named is None:
            text, _too = reader.read_until(b"\r\n--" + token)
            if name in {"csrf", "library", "folder", "relative", "source", "op", "decision", "conflict"} and len(text) <= 4000:
                fields[name] = text.decode("utf-8", "replace")
            continue
        filename = named.group(1).decode("utf-8", "replace")
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f"part-{secrets.token_hex(8)}"
        with target.open("wb") as sink:
            _body, too_big = reader.read_until(b"\r\n--" + token, sink=sink, limit=MEDIA_LIMIT + 1)
        files.append({"name": filename, "path": target, "too_big": too_big})
    return {"fields": fields, "files": files}

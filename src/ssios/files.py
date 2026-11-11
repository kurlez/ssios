"""Per-account files. Bytes stay under the state directory, never in the repo.

Images are identified by their magic bytes. EXIF and other metadata chunks are
removed before anything is written. The same sha256 is stored once.
"""

from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime, timezone

from ssios.errors import SsiosError
from ssios.util import read_json, secure_mkdir, write_json, write_nofollow

_MAX_BYTES = 512_000
_LOCK = threading.Lock()
_ID = re.compile(r"file-[0-9a-f]{16}")
_TAG = re.compile(r"[A-Za-z0-9-]{1,24}")
_KEEP_PNG = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS"}


def screenshot_request(message: str) -> dict | None:
    """A request to file the current screenshot. None when it is not one."""
    text = " ".join(str(message or "").split())
    folded = text.casefold()
    if "保存这张截图" not in text and "保存这张图片" not in text and not re.search(r"\bsave this (screenshot|image)\b", folded):
        return None
    title = ""
    found = re.search(r"(?:标题|title)\s*[:：]\s*(.+?)(?:\s+(?:标签|tags)\b|$)", text, re.IGNORECASE)
    if found:
        title = found.group(1).strip(" ，,。.")
    tags = _TAG.findall(text)
    for noise in ("title", "tags"):
        tags = [tag for tag in tags if tag.casefold() != noise]
    extra = re.search(r"(?:标签|tags)\s*[:：]\s*([A-Za-z0-9, -]{1,80})", text, re.IGNORECASE)
    if extra:
        tags.extend(_TAG.findall(extra.group(1)))
    cleaned = []
    for tag in tags:
        token = tag.casefold()
        if token not in cleaned and token not in {"png", "jpg", "gif", "pdf"}:
            cleaned.append(token)
    return {"title": title[:80], "tags": cleaned[:8]}


class FileLibrary:
    def __init__(self, plane) -> None:
        self.plane = plane

    def add(
        self,
        account_id: str,
        raw: bytes,
        *,
        name: str = "",
        origin: str = "uploaded",
        source: str = "upload",
        thread_id: str = "",
        message_id: str = "",
        tags: list[str] | None = None,
        peer_ip: str = "127.0.0.1",
    ) -> dict:
        if origin not in {"uploaded", "generated"}:
            raise SsiosError("bad_request", "that file was rejected", 400)
        cleaned = _prepare(raw, trust=origin == "generated")
        digest = hashlib.sha256(cleaned).hexdigest()
        with _LOCK:
            rows = self._rows(account_id)
            for row in rows:
                if row.get("sha256") == digest and not row.get("trashed"):
                    self._audit(account_id, peer_ip, row["id"], "duplicate", len(cleaned))
                    public = _public(row)
                    public["duplicate"] = True
                    return public
            file_id = "file-" + digest[:16]
            while any(row.get("id") == file_id for row in rows):
                file_id = "file-" + hashlib.sha256(file_id.encode()).hexdigest()[:16]
            ext = cleaned and _kind(cleaned)["ext"]
            folder = self._root(account_id) / "objects"
            secure_mkdir(folder)
            path = folder / f"{digest}.{ext}"
            if not path.exists():
                path.write_bytes(cleaned)
                path.chmod(0o600)
            record = {
                "id": file_id,
                "name": _filename(name, ext),
                "tags": _tags(tags),
                "origin": origin,
                "source": source if source in {"upload", "clip", "report", "export", "assistant", "screenshot"} else "upload",
                "role": _kind(cleaned)["role"],
                "content_type": _kind(cleaned)["type"],
                "ext": ext,
                "sha256": digest,
                "size": len(cleaned),
                "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "thread_id": thread_id if re.fullmatch(r"conv-[0-9a-f]{16}", thread_id or "") else "",
                "message_id": message_id if re.fullmatch(r"msg-[0-9a-f]{16}", message_id or "") else "",
                "trashed": False,
            }
            rows.append(record)
            self._write(account_id, rows)
        self._audit(account_id, peer_ip, file_id, "saved", len(cleaned))
        public = _public(record)
        public["duplicate"] = False
        return public

    def attach(self, account_id: str, file_id: str, *, thread_id: str, message_id: str) -> None:
        with _LOCK:
            rows = self._rows(account_id)
            for row in rows:
                if row.get("id") == file_id:
                    if re.fullmatch(r"conv-[0-9a-f]{16}", thread_id or ""):
                        row["thread_id"] = thread_id
                    if re.fullmatch(r"msg-[0-9a-f]{16}", message_id or ""):
                        row["message_id"] = message_id
                    self._write(account_id, rows)
                    return

    def get(self, account_id: str, file_id: str) -> dict:
        row = self._one(account_id, file_id)
        return _public(row)

    def bytes(self, account_id: str, file_id: str) -> tuple[bytes, str, str]:
        row = self._one(account_id, file_id)
        if row.get("trashed"):
            raise SsiosError("not_found", "file not found", 404)
        path = self._root(account_id) / "objects" / f"{row['sha256']}.{row['ext']}"
        if not path.is_file() or path.is_symlink():
            raise SsiosError("not_found", "file not found", 404)
        return path.read_bytes(), row["content_type"], row["name"]

    def list(
        self,
        account_id: str,
        *,
        origin: str = "",
        role: str = "",
        day: str = "",
        thread_id: str = "",
        query: str = "",
        trash: bool = False,
    ) -> list[dict]:
        needle = query.casefold().strip()
        found = []
        for row in self._rows(account_id):
            if bool(row.get("trashed")) != trash:
                continue
            if origin and row.get("origin") != origin:
                continue
            if role and row.get("role") != role:
                continue
            if day and not str(row.get("created_at") or "").startswith(day):
                continue
            if thread_id and row.get("thread_id") != thread_id:
                continue
            if needle:
                blob = (str(row.get("name") or "") + " " + " ".join(row.get("tags") or [])).casefold()
                if needle not in blob:
                    continue
            found.append(_public(row))
        return list(reversed(found))

    def rename(self, account_id: str, file_id: str, name: str, peer_ip: str = "127.0.0.1") -> dict:
        with _LOCK:
            rows = self._rows(account_id)
            row = _find(rows, file_id)
            row["name"] = _filename(name, row["ext"])
            self._write(account_id, rows)
        self._audit(account_id, peer_ip, file_id, "renamed", int(row["size"]))
        return _public(row)

    def retag(self, account_id: str, file_id: str, tags: list[str], peer_ip: str = "127.0.0.1") -> dict:
        with _LOCK:
            rows = self._rows(account_id)
            row = _find(rows, file_id)
            row["tags"] = _tags(tags)
            self._write(account_id, rows)
        self._audit(account_id, peer_ip, file_id, "tagged", int(row["size"]))
        return _public(row)

    def trash(self, account_id: str, file_id: str, peer_ip: str = "127.0.0.1") -> dict:
        return self._flag(account_id, file_id, True, peer_ip, "trashed")

    def restore(self, account_id: str, file_id: str, peer_ip: str = "127.0.0.1") -> dict:
        return self._flag(account_id, file_id, False, peer_ip, "restored")

    def purge(self, account_id: str, file_id: str, confirm: str, peer_ip: str = "127.0.0.1") -> None:
        if confirm != "delete":
            raise SsiosError("bad_request", "type delete to remove the file", 400)
        with _LOCK:
            rows = self._rows(account_id)
            row = _find(rows, file_id)
            if not row.get("trashed"):
                raise SsiosError("bad_request", "move the file to trash first", 400)
            kept = [item for item in rows if item.get("id") != file_id]
            if not any(item.get("sha256") == row.get("sha256") for item in kept):
                path = self._root(account_id) / "objects" / f"{row['sha256']}.{row['ext']}"
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            self._write(account_id, kept)
        self._audit(account_id, peer_ip, file_id, "purged", int(row["size"]))

    def file_screenshot(self, account_id: str, file_id: str, *, title: str, tags: list[str], peer_ip: str = "127.0.0.1") -> dict:
        """Copy the image into the account vault and write a note that embeds it."""
        from ssios.vault import account_vault, slugify

        row = self._one(account_id, file_id)
        if row.get("role") != "image" or row.get("trashed"):
            raise SsiosError("bad_request", "that file is not an image", 400)
        raw, _content_type, _name = self.bytes(account_id, file_id)
        label = title or re.sub(r"\.[A-Za-z0-9]+$", "", row["name"]) or "Screenshot"
        slug = slugify(label)
        root = account_vault(self.plane.settings, account_id)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        asset = f"attachments/{slug}.{row['ext']}"
        note = f"Clippings/{slug}.md"
        if (root / note).exists():
            slug = slug + "-2"
            asset = f"attachments/{slug}.{row['ext']}"
            note = f"Clippings/{slug}.md"
        write_nofollow(root, asset, raw, mode=0o644)
        tag_line = "\n".join(f"  - {tag}" for tag in _tags(tags or row.get("tags") or ["screenshot"]))
        body = (
            f"---\ntitle: {label}\ntags:\n{tag_line or '  - screenshot'}\nsource: chat\n---\n\n"
            f"![[attachments/{slug}.{row['ext']}]]\n"
        )
        write_nofollow(root, note, body.encode("utf-8"), mode=0o644)
        self.add(account_id, body.encode("utf-8"), name=f"{slug}.md", origin="generated", source="screenshot", peer_ip=peer_ip, tags=tags)
        self._audit(account_id, peer_ip, file_id, "filed", len(raw))
        return {"note": note, "asset": asset, "title": label}

    def _flag(self, account_id: str, file_id: str, trashed: bool, peer_ip: str, result: str) -> dict:
        with _LOCK:
            rows = self._rows(account_id)
            row = _find(rows, file_id)
            row["trashed"] = trashed
            self._write(account_id, rows)
        self._audit(account_id, peer_ip, file_id, result, int(row["size"]))
        return _public(row)

    def _one(self, account_id: str, file_id: str) -> dict:
        if not _ID.fullmatch(file_id or ""):
            raise SsiosError("not_found", "file not found", 404)
        return _find(self._rows(account_id), file_id)

    def _rows(self, account_id: str) -> list[dict]:
        data = read_json(self._index(account_id), {"files": []})
        rows = data.get("files") if isinstance(data, dict) else []
        return [row for row in rows if isinstance(row, dict)]

    def _write(self, account_id: str, rows: list[dict]) -> None:
        write_json(self._index(account_id), {"files": rows})

    def _index(self, account_id: str):
        return self._root(account_id) / "index.json"

    def _root(self, account_id: str):
        return self.plane.accounts.home(account_id) / "files"

    def _audit(self, account_id: str, peer_ip: str, file_id: str, result: str, size: int) -> None:
        self.plane.security_audit.write(
            actor=account_id,
            ip=peer_ip or "127.0.0.1",
            action="file.upload" if result in {"saved", "duplicate"} else "file.change",
            target=file_id,
            result=result,
            extra={"service": "files", "byte_count": str(size)},
        )


def _prepare(raw: bytes, *, trust: bool = False) -> bytes:
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_BYTES:
        raise SsiosError("bad_request", "that file was rejected", 400)
    kind = _kind(raw)
    if kind["role"] == "image":
        stripped = _strip(raw)
        if _kind(stripped)["role"] != "image" or len(stripped) > _MAX_BYTES:
            raise SsiosError("bad_request", "that file was rejected", 400)
        return stripped
    folded = raw.lower()
    if not trust and kind["role"] in {"text", "markdown"} and (b"\x00" in raw or b"<script" in folded or b"<svg" in folded):
        raise SsiosError("bad_request", "that file was rejected", 400)
    return raw


def _kind(raw: bytes) -> dict:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return {"ext": "png", "type": "image/png", "role": "image"}
    if raw.startswith(b"\xff\xd8\xff"):
        return {"ext": "jpg", "type": "image/jpeg", "role": "image"}
    if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
        return {"ext": "gif", "type": "image/gif", "role": "image"}
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return {"ext": "webp", "type": "image/webp", "role": "image"}
    if raw.startswith(b"%PDF-"):
        return {"ext": "pdf", "type": "application/pdf", "role": "pdf"}
    if raw.startswith(b"---\n") or raw.startswith(b"#"):
        return {"ext": "md", "type": "text/markdown", "role": "markdown"}
    try:
        raw.decode("utf-8")
    except UnicodeError as exc:
        raise SsiosError("bad_request", "that file was rejected", 400) from exc
    sample = raw[:200].lstrip().lower()
    if sample.startswith(b"<") or sample.startswith(b"<!doctype"):
        raise SsiosError("bad_request", "that file was rejected", 400)
    return {"ext": "txt", "type": "text/plain", "role": "text"}


def _filename(name: str, ext: str) -> str:
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._ -]", "", base).strip(" .")[:80]
    if not base or base.startswith("."):
        base = "file." + ext
    if not base.casefold().endswith("." + ext):
        stem = base.rsplit(".", 1)[0]
        base = stem + "." + ext
    return base


def _tags(tags: list[str] | None) -> list[str]:
    cleaned = []
    for tag in tags or []:
        for token in _TAG.findall(str(tag)):
            token = token.casefold()
            if token not in cleaned:
                cleaned.append(token)
    return cleaned[:8]


def _public(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "tags": list(row.get("tags") or []),
        "origin": row.get("origin") or "uploaded",
        "source": row.get("source") or "upload",
        "role": row.get("role") or "text",
        "content_type": row.get("content_type") or "application/octet-stream",
        "size": int(row.get("size") or 0),
        "created_at": str(row.get("created_at") or ""),
        "thread_id": str(row.get("thread_id") or ""),
        "message_id": str(row.get("message_id") or ""),
        "trashed": bool(row.get("trashed")),
        "url": "/ui/files/" + row["id"] + "/raw",
        "sha256": row.get("sha256") or "",
    }


def _find(rows: list[dict], file_id: str) -> dict:
    for row in rows:
        if row.get("id") == file_id:
            return row
    raise SsiosError("not_found", "file not found", 404)


def _strip(raw: bytes) -> bytes:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return _strip_png(raw)
    if raw.startswith(b"\xff\xd8\xff"):
        return _strip_jpeg(raw)
    if raw.startswith(b"GIF8"):
        return _strip_gif(raw)
    if raw.startswith(b"RIFF") and len(raw) > 12 and raw[8:12] == b"WEBP":
        return _strip_webp(raw)
    raise SsiosError("bad_request", "that file was rejected", 400)


def _strip_png(raw: bytes) -> bytes:
    if not any(marker in raw for marker in (b"tEXt", b"zTXt", b"iTXt", b"eXIf")):
        return raw
    out = bytearray(raw[:8])
    pos = 8
    saw_end = False
    while pos + 8 <= len(raw):
        length = int.from_bytes(raw[pos : pos + 4], "big")
        kind = raw[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(raw):
            break
        if kind in _KEEP_PNG:
            out.extend(raw[pos:end])
        if kind == b"IEND":
            saw_end = True
            break
        pos = end
    if not saw_end:
        raise SsiosError("bad_request", "that file was rejected", 400)
    return bytes(out)


def _strip_jpeg(raw: bytes) -> bytes:
    out = bytearray(b"\xff\xd8")
    pos = 2
    while pos + 1 < len(raw):
        if raw[pos] != 0xFF:
            break
        marker = raw[pos + 1]
        if marker == 0xD9:
            out.extend(b"\xff\xd9")
            break
        if marker == 0xDA:
            out.extend(raw[pos:])
            break
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if pos + 4 > len(raw):
            break
        length = int.from_bytes(raw[pos + 2 : pos + 4], "big")
        if length < 2 or pos + 2 + length > len(raw):
            break
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE):
            out.extend(raw[pos : pos + 2 + length])
        pos += 2 + length
    if not out.startswith(b"\xff\xd8"):
        raise SsiosError("bad_request", "that file was rejected", 400)
    return bytes(out)


def _strip_gif(raw: bytes) -> bytes:
    out = bytearray()
    pos = 0
    while pos < len(raw):
        if raw[pos : pos + 2] == b"\x21\xfe" or raw[pos : pos + 2] == b"\x21\x01" or raw[pos : pos + 2] == b"\x21\xff":
            pos += 2
            while pos < len(raw):
                size = raw[pos]
                pos += 1 + size
                if size == 0:
                    break
            continue
        out.append(raw[pos])
        pos += 1
    return bytes(out)


def _strip_webp(raw: bytes) -> bytes:
    if len(raw) < 12:
        raise SsiosError("bad_request", "that file was rejected", 400)
    chunks = bytearray()
    pos = 12
    while pos + 8 <= len(raw):
        kind = raw[pos : pos + 4]
        size = int.from_bytes(raw[pos + 4 : pos + 8], "little")
        end = pos + 8 + size + (size % 2)
        if end > len(raw):
            break
        if kind not in {b"EXIF", b"XMP "}:
            chunks.extend(raw[pos:end])
        pos = end
    payload = b"WEBP" + bytes(chunks)
    return b"RIFF" + len(payload).to_bytes(4, "little") + payload

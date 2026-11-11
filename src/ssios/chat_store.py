"""Per-account conversation files under the state directory.

Nothing here is written into the git repository. Conversation ids are a fixed
shape so a path cannot escape the account chat directory.
"""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path

from ssios.errors import SsiosError
from ssios.util import read_json, secure_mkdir, write_json

_CONV = re.compile(r"^conv-[0-9a-f]{16}$")
_MSG = re.compile(r"^msg-[0-9a-f]{16}$")


class ChatStore:
    def __init__(self, home: Path) -> None:
        self.root = home / "chat"
        secure_mkdir(self.root)

    def list_conversations(self) -> list[dict]:
        index = read_json(self.root / "index.json", {"ids": []})
        ids = index.get("ids") if isinstance(index, dict) else []
        rows = []
        for conv_id in ids if isinstance(ids, list) else []:
            try:
                record = self.get(str(conv_id))
            except SsiosError:
                continue
            rows.append(
                {
                    "id": record["id"],
                    "title": record.get("title") or "Conversation",
                    "updated_at": record.get("updated_at"),
                    "created_at": record.get("created_at"),
                }
            )
        return rows

    def create(self, title: str) -> dict:
        conv_id = "conv-" + secrets.token_hex(8)
        record = {
            "id": conv_id,
            "title": _title(title),
            "created_at": time.time(),
            "updated_at": time.time(),
            "messages": [],
        }
        self._write(record)
        index = read_json(self.root / "index.json", {"ids": []})
        ids = index.get("ids") if isinstance(index, dict) else []
        if not isinstance(ids, list):
            ids = []
        ids.append(conv_id)
        write_json(self.root / "index.json", {"ids": ids})
        return record

    def find_approval(self, approval_id: str) -> tuple[str, dict] | None:
        index = read_json(self.root / "index.json", {"ids": []})
        ids = index.get("ids") if isinstance(index, dict) else []
        for conv_id in ids if isinstance(ids, list) else []:
            try:
                record = self.get(str(conv_id))
            except SsiosError:
                continue
            for message in record.get("messages") or []:
                if message.get("approval_id") == approval_id:
                    return record["id"], message
        return None

    def get(self, conv_id: str) -> dict:
        path = self._path(conv_id)
        data = read_json(path, None)
        if not isinstance(data, dict) or data.get("id") != conv_id:
            raise SsiosError("not_found", "conversation not found", 404)
        return data

    def rename(self, conv_id: str, title: str) -> dict:
        record = self.get(conv_id)
        record["title"] = _title(title)
        record["updated_at"] = time.time()
        self._write(record)
        return {"id": record["id"], "title": record["title"]}

    def delete(self, conv_id: str) -> None:
        path = self._path(conv_id)
        if path.is_file():
            path.unlink()
        index = read_json(self.root / "index.json", {"ids": []})
        ids = index.get("ids") if isinstance(index, dict) else []
        if isinstance(ids, list):
            write_json(self.root / "index.json", {"ids": [item for item in ids if item != conv_id]})

    def set_pending(self, conv_id: str, pending: dict | None) -> None:
        record = self.get(conv_id)
        if pending is None:
            record.pop("pending_pick", None)
        else:
            record["pending_pick"] = pending
        record["updated_at"] = time.time()
        self._write(record)

    def append(self, conv_id: str, message: dict) -> dict:
        record = self.get(conv_id)
        message = dict(message)
        message.setdefault("id", "msg-" + secrets.token_hex(8))
        if not _MSG.fullmatch(str(message["id"])):
            raise SsiosError("path_rejected", "message id rejected", 400)
        message["at"] = time.time()
        record["messages"].append(message)
        record["updated_at"] = message["at"]
        if record["messages"] and record["messages"][0].get("role") == "user" and not record.get("title"):
            record["title"] = _title(str(record["messages"][0].get("text") or ""))
        self._write(record)
        return message

    def _path(self, conv_id: str) -> Path:
        if not _CONV.fullmatch(conv_id):
            raise SsiosError("path_rejected", "conversation id rejected", 400)
        path = (self.root / f"{conv_id}.json").resolve()
        if self.root.resolve() != path.parent:
            raise SsiosError("path_rejected", "conversation path rejected", 400)
        return path

    def _write(self, record: dict) -> None:
        write_json(self._path(str(record["id"])), record)


def _title(text: str) -> str:
    flat = " ".join(str(text or "").split())
    if not flat:
        return "Conversation"
    return flat[:48]

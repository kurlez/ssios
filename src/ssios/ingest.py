"""Stage local markdown notes as a memory commit. Nothing is written before yes."""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path

from ssios.errors import SsiosError
from ssios.summary import CONFIRM, _base_url, _call, _decide, _emit, _human_token, _print_diff, _require_identity
from ssios.util import validate_rel_parts

MAX_FILES = 32
MAX_FILE_BYTES = 48_000
MAX_TOTAL_BYTES = 256_000
_ALLOWED = {".md", ".txt"}
_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
    r"|\bsk-[A-Za-z0-9]{20,}\b"
    r"|\b(?i:api[_-]?key|secret|password|token)\b\s*[:=]\s*\S{12,}"
)
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def collect_notes(source: Path) -> list[tuple[str, str]]:
    """Return (relative path, utf-8 text) for allowlisted files. Symlinks are rejected."""
    root = source.expanduser()
    if root.is_symlink():
        raise SsiosError("symlink_rejected", "symlink rejected", 400)
    if not root.exists():
        raise SsiosError("not_found", "ingest source was not found", 404)
    root_real = root.resolve()
    found: list[tuple[str, str]] = []
    if root_real.is_file():
        found.append((root_real.name, _read_note(root_real, root_real)))
    elif root_real.is_dir():
        for dirpath, dirnames, filenames in os.walk(root_real, followlinks=False):
            current = Path(dirpath)
            kept = []
            for name in dirnames:
                child = current / name
                if child.is_symlink() or name.startswith("."):
                    if child.is_symlink():
                        raise SsiosError("symlink_rejected", "symlink rejected", 400)
                    continue
                kept.append(name)
            dirnames[:] = kept
            for name in sorted(filenames):
                path = current / name
                if path.is_symlink():
                    raise SsiosError("symlink_rejected", "symlink rejected", 400)
                if path.suffix.lower() not in _ALLOWED:
                    continue
                relative = path.resolve().relative_to(root_real).as_posix()
                found.append((relative, _read_note(path, root_real)))
    else:
        raise SsiosError("path_rejected", "ingest source must be a file or directory", 400)
    if not found:
        raise SsiosError("no_content", "no markdown or text notes were found", 400)
    if len(found) > MAX_FILES:
        raise SsiosError("ingest_too_large", "too many notes", 400)
    total = sum(len(text.encode("utf-8")) for _path, text in found)
    if total > MAX_TOTAL_BYTES:
        raise SsiosError("ingest_too_large", "notes are too large", 400)
    return found


def notes_to_memory(project_id: str, notes: list[tuple[str, str]], existing_index: str | None) -> list[dict]:
    if not _PROJECT.fullmatch(project_id):
        raise SsiosError("project_invalid", "project id rejected", 400)
    files = []
    index_lines = ["# Notes", ""]
    for relative, text in notes:
        dest = _destination(project_id, relative)
        document = _as_markdown(dest, text)
        _reject_secrets(document)
        files.append({"path": dest, "content": document})
        title = dest.rsplit("/", 1)[-1]
        index_lines.append(f"- {title}")
    index_path = f"topics/{project_id}/MEMORY.md"
    merged = _merge_index(existing_index, index_lines)
    files.append({"path": index_path, "content": merged})
    return files


def run_ingest(settings, source: Path, project_id: str) -> int:
    if not _PROJECT.fullmatch(project_id):
        raise SsiosError("project_invalid", "project id rejected", 400)
    notes = collect_notes(source)
    token = _human_token(settings)
    base = _base_url(settings)
    _require_identity(settings, base, token)
    quoted_project = urllib.parse.quote(project_id)
    code, core, _headers = _call(
        base,
        "GET",
        f"/v1/memory/core?project_id={quoted_project}",
        token=token,
        timeout=30,
    )
    if code != 200:
        raise SsiosError(str(core.get("error") or "memory_unavailable"), str(core.get("detail") or "memory core failed"))
    revision = str(core.get("revision") or "")
    index_path = f"topics/{project_id}/MEMORY.md"
    code, current, _headers = _call(
        base,
        "GET",
        f"/v1/memory/file?project_id={quoted_project}&path={urllib.parse.quote(index_path, safe='')}",
        token=token,
        timeout=30,
    )
    existing = current.get("content") if code == 200 and isinstance(current, dict) else None
    files = notes_to_memory(project_id, notes, existing if isinstance(existing, str) else None)
    code, proposed, _headers = _call(
        base,
        "POST",
        "/v1/memory/propose",
        token=token,
        body={"project_id": project_id, "expected_revision": revision, "files": files},
        timeout=30,
    )
    if code != 200:
        raise SsiosError(str(proposed.get("error") or "propose_failed"), str(proposed.get("detail") or "propose failed"))
    approval = proposed.get("approval") if isinstance(proposed.get("approval"), dict) else {}
    approval_id = str(approval.get("id") or "")
    code, preview, _headers = _call(base, "GET", f"/v1/approvals/{approval_id}", token=token, timeout=30)
    if code != 200:
        raise SsiosError(str(preview.get("error") or "preview_failed"), str(preview.get("detail") or "preview failed"))
    print(
        f"Pending {preview.get('action')} payload {preview.get('payload_sha256')} expected revision {preview.get('expected_revision')}",
        flush=True,
    )
    for item in preview.get("files") or []:
        path = str(item.get("path") or "")
        after = str(item.get("content") or "")
        before = existing if path == index_path and isinstance(existing, str) else None
        _print_diff(path, before, after)
    print(CONFIRM, flush=True)
    import sys

    line = sys.stdin.readline()
    if line == "":
        _emit({"result": "pending", "approval_id": approval_id, "detail": "No input. The approval stays pending and memory was not written."})
        return 2
    choice = line.strip()
    if choice == "no":
        _decide(base, token, preview, "deny", 60)
        _emit({"result": "denied", "approval_id": approval_id, "project_id": project_id})
        return 0
    if choice != "yes":
        _emit({"result": "pending", "approval_id": approval_id, "detail": "yes was not entered. The approval stays pending and memory was not written."})
        return 2
    _decide(base, token, preview, "approve", 90)
    _emit({"result": "committed", "approval_id": approval_id, "project_id": project_id, "files": len(files)})
    return 0


def _read_note(path: Path, root_real: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SsiosError("symlink_rejected", "symlink rejected", 400)
    resolved = path.resolve()
    if root_real != resolved and root_real not in resolved.parents:
        raise SsiosError("path_rejected", "path escaped the ingest source", 400)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SsiosError("ingest_too_large", "a note is too large", 400)
    data = path.read_bytes()
    if b"\x00" in data:
        raise SsiosError("path_rejected", "note is not text", 400)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SsiosError("path_rejected", "note is not utf-8", 400) from exc
    _reject_secrets(text)
    return text


def _destination(project_id: str, relative: str) -> str:
    suffix = Path(relative).suffix.lower()
    if suffix not in _ALLOWED:
        raise SsiosError("path_rejected", "only md and txt notes are accepted", 400)
    stem = relative[: -len(suffix)] + ".md"
    if stem.endswith("/MEMORY.md") or stem == "MEMORY.md" or stem.rsplit("/", 1)[-1] == "MEMORY.md":
        raise SsiosError("path_rejected", "MEMORY.md is reserved for the generated index", 400)
    dest = f"topics/{project_id}/{stem}"
    validate_rel_parts(dest)
    return dest


def _as_markdown(dest: str, text: str) -> str:
    body = text.replace("\r\n", "\n").strip() + "\n"
    _reject_secrets(body)
    if body.startswith("---\n"):
        return body
    name = dest.rsplit("/", 1)[-1].removesuffix(".md")
    return f"---\nname: {name}\ndescription: \"Imported note\"\n---\n\n{body}"


def _reject_secrets(text: str) -> None:
    if _SECRET.search(text):
        raise SsiosError("secret_rejected", "note looks like it contains a secret and was not staged", 400)


def _merge_index(existing: str | None, generated: list[str]) -> str:
    lines = [line for line in generated if line != "# Notes" and line != ""]
    if not existing:
        return "# Notes\n\n" + "\n".join(lines) + "\n"
    merged = existing
    if not merged.endswith("\n"):
        merged += "\n"
    for line in lines:
        if line not in merged:
            merged += line + "\n"
    return merged

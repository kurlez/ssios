"""Installable skills in the Agent Skills layout: a folder with a SKILL.md and, optionally, references and scripts.

    my-skill/
      SKILL.md          ---\\nname: my-skill\\ndescription: What it does and when to use it.\\n---\\n instructions…
      reference.md      read by the model when SKILL.md points to it
      scripts/run.py    run only in the sandbox, only when the owner allowed scripts, and each run approved

A skill comes from a link (a SKILL.md, a .zip, or a GitHub folder), is unpacked into a staging folder, scanned
(``skill_scan``), and installed into the account's home only when the owner confirms after reading the report.
Nothing in a skill runs at install time.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import shutil
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath

from ssios.errors import SsiosError

NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
MAX_FILES = 300
MAX_FILE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 10_000_000
MAX_DOWNLOAD = 20_000_000
META = ".ssios-skill.json"
SCRIPT_EXT = {".py": "python", ".sh": "shell", ".bash": "shell", ".js": "node", ".mjs": "node"}


def _err(text: str, status: int = 400) -> SsiosError:
    return SsiosError("skill_rejected", text, status)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """The YAML header of SKILL.md: simple ``key: value`` lines, quoted values, and ``>`` or ``|`` blocks."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return {}, text
    found: dict = {}
    key = ""
    block: list[str] = []
    for raw in lines[1:end]:
        if key and (raw.startswith(" ") or raw.startswith("\t") or not raw.strip()):
            block.append(raw.strip())
            continue
        if key:
            found[key] = " ".join(part for part in block if part).strip()
            key, block = "", []
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", raw)
        if not match:
            continue
        name, value = match.group(1).strip(), match.group(2).strip()
        if value in {">", "|", ">-", "|-"}:
            key, block = name, []
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[name] = value
    if key:
        found[key] = " ".join(part for part in block if part).strip()
    return found, "\n".join(lines[end + 1 :])


def _clean_path(name: str) -> str | None:
    """A safe relative path inside the skill, or None for one that is not."""
    text = str(name or "").replace("\\", "/")
    if not text or text.startswith("/") or "\x00" in text or re.match(r"^[A-Za-z]:", text):
        return None
    parts = [part for part in PurePosixPath(text).parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def from_zip(raw: bytes, subpath: str = "") -> dict[str, bytes]:
    """Files of the skill inside a zip. With ``subpath``, only that folder. Refuses links, escapes, and bombs."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise _err("That link is not a zip file or a SKILL.md.") from exc
    entries = [info for info in archive.infolist() if not info.is_dir()]
    if len(entries) > 5000:
        raise _err("The archive has too many files.")
    names: dict[str, zipfile.ZipInfo] = {}
    for info in entries:
        if stat.S_ISLNK(info.external_attr >> 16):
            raise _err(f"The archive holds a link ({info.filename[:80]}). Skills with links are refused.")
        path = _clean_path(info.filename)
        if path is None:
            raise _err("The archive has a path that leaves its folder.")
        names[path] = info
    # Where is SKILL.md? A GitHub archive puts everything under one top folder.
    prefix = ""
    wanted = _clean_path(subpath) if subpath else ""
    if wanted:
        candidates = [path for path in names if path == f"{wanted}/SKILL.md" or path.endswith(f"/{wanted}/SKILL.md")]
    else:
        candidates = sorted((path for path in names if path.rsplit("/", 1)[-1] == "SKILL.md"), key=lambda path: path.count("/"))
    if not candidates:
        raise _err("No SKILL.md was found there.")
    if not wanted and len(candidates) > 1 and candidates[0].count("/") == candidates[1].count("/"):
        raise _err("That archive holds several skills. Link to one skill's folder.")
    prefix = candidates[0][: -len("SKILL.md")]
    files: dict[str, bytes] = {}
    total = 0
    for path, info in names.items():
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix) :]
        if info.file_size > MAX_FILE_BYTES:
            raise _err(f"{relative} is larger than {MAX_FILE_BYTES // 1_000_000} MB.")
        if info.compress_size and info.file_size / max(info.compress_size, 1) > 200:
            raise _err("The archive is compressed in a suspicious way.")
        total += info.file_size
        if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
            raise _err("The skill is larger than SSIOS accepts.")
        data = archive.read(info)
        if len(data) != info.file_size:
            raise _err("The archive is damaged.")
        files[relative] = data
    return files


def validate(files: dict[str, bytes]) -> dict:
    """Name, description, and body of a skill. Raises when SKILL.md is missing or its header is wrong."""
    if "SKILL.md" not in files:
        raise _err("No SKILL.md was found there.")
    try:
        text = files["SKILL.md"].decode("utf-8")
    except UnicodeError as exc:
        raise _err("SKILL.md is not UTF-8 text.") from exc
    head, body = parse_frontmatter(text)
    name = str(head.get("name") or "").strip()
    description = " ".join(str(head.get("description") or "").split())
    if not NAME.fullmatch(name):
        raise _err("SKILL.md needs a name of lowercase letters, digits, and hyphens, up to 64 characters.")
    # The format asks for 1024 characters; some published skills run a little longer, so up to 4000 is kept.
    if not description or len(description) > 4000:
        raise _err("SKILL.md needs a description of up to 4000 characters.")
    return {"name": name, "description": description, "body": body, "header": head}


def scripts_of(files: dict[str, bytes]) -> list[str]:
    return sorted(path for path in files if Path(path).suffix.casefold() in SCRIPT_EXT)


# Where skills live.


def home_of(plane, account_id: str) -> Path:
    return plane.accounts.home(account_id) / "skills"


def _staging(plane, account_id: str) -> Path:
    return plane.accounts.home(account_id) / "skills-staging"


def stage(plane, account_id: str, files: dict[str, bytes], source: str, report: dict) -> dict:
    info = validate(files)
    stage_id = "stg-" + secrets.token_hex(6)
    folder = _staging(plane, account_id) / stage_id
    _write_tree(folder / "files", files)
    record = {
        "id": stage_id,
        "name": info["name"],
        "description": info["description"],
        "source": source[:500],
        "scripts": scripts_of(files),
        "report": report,
        "staged_at": time.time(),
        "sha256": _digest(files),
    }
    (folder / "stage.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return record


def staged(plane, account_id: str, stage_id: str) -> tuple[dict, dict[str, bytes]]:
    if not re.fullmatch(r"stg-[0-9a-f]{12}", str(stage_id or "")):
        raise _err("That install is no longer waiting.", 404)
    folder = _staging(plane, account_id) / stage_id
    try:
        record = json.loads((folder / "stage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _err("That install is no longer waiting.", 404) from exc
    return record, _read_tree(folder / "files")


def drop_stage(plane, account_id: str, stage_id: str) -> None:
    if re.fullmatch(r"stg-[0-9a-f]{12}", str(stage_id or "")):
        shutil.rmtree(_staging(plane, account_id) / stage_id, ignore_errors=True)


def install(plane, account_id: str, stage_id: str, *, allow_scripts: bool, accept_warnings: bool) -> dict:
    record, files = staged(plane, account_id, stage_id)
    level = str((record.get("report") or {}).get("level") or "high")
    if level == "high":
        raise _err("This skill failed the safety check and cannot be installed.", 403)
    if level == "medium" and not accept_warnings:
        raise _err("Read the warnings and tick the box to install this skill.")
    if _digest(files) != record.get("sha256"):
        raise _err("The staged files changed. Fetch the skill again.", 409)
    target = home_of(plane, account_id) / record["name"]
    if target.exists():
        shutil.rmtree(target)
    _write_tree(target, files)
    meta = {
        "name": record["name"],
        "description": record["description"],
        "source": record["source"],
        "sha256": record["sha256"],
        "installed_at": time.time(),
        "enabled": True,
        "scripts_allowed": bool(allow_scripts) and bool(record.get("scripts")),
        "scripts": record.get("scripts") or [],
        "report": record.get("report") or {},
    }
    (target / META).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    drop_stage(plane, account_id, stage_id)
    return meta


def installed(plane, account_id: str) -> list[dict]:
    root = home_of(plane, account_id)
    if not root.is_dir():
        return []
    rows = []
    for folder in sorted(root.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or not NAME.fullmatch(folder.name):
            continue
        try:
            meta = json.loads((folder / META).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("name") == folder.name:
            rows.append(meta)
    return rows


def get(plane, account_id: str, name: str) -> dict:
    if not NAME.fullmatch(str(name or "")):
        raise _err("No installed skill has that name.", 404)
    for row in installed(plane, account_id):
        if row["name"] == name:
            return row
    raise _err("No installed skill has that name.", 404)


def update(plane, account_id: str, name: str, **changes) -> dict:
    meta = get(plane, account_id, name)
    if "enabled" in changes:
        meta["enabled"] = bool(changes["enabled"])
    if "scripts_allowed" in changes:
        meta["scripts_allowed"] = bool(changes["scripts_allowed"]) and bool(meta.get("scripts"))
    (home_of(plane, account_id) / name / META).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def remove(plane, account_id: str, name: str) -> None:
    get(plane, account_id, name)
    shutil.rmtree(home_of(plane, account_id) / name)


def read_file(plane, account_id: str, name: str, path: str = "SKILL.md", limit: int = 40_000) -> str:
    """Text of one file of an enabled skill, for the model."""
    meta = get(plane, account_id, name)
    if not meta.get("enabled"):
        raise _err("That skill is turned off.", 403)
    relative = _clean_path(path or "SKILL.md")
    if relative is None or relative == META:
        raise _err("That file is not part of the skill.", 404)
    target = home_of(plane, account_id) / name / relative
    if target.is_symlink() or not target.is_file():
        raise _err("That file is not part of the skill.", 404)
    try:
        text = target.read_bytes()[:limit].decode("utf-8")
    except UnicodeError as exc:
        raise _err("That file is not text.", 400) from exc
    return text


def files_of(plane, account_id: str, name: str) -> list[str]:
    get(plane, account_id, name)
    return sorted(path for path in _read_tree(home_of(plane, account_id) / name) if path != META)


def _write_tree(folder: Path, files: dict[str, bytes]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for relative, data in files.items():
        path = _clean_path(relative)
        if path is None:
            raise _err("A file path leaves the skill's folder.")
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        # Nothing from a skill is executable on disk. The sandbox runs scripts through an interpreter.
        target.chmod(0o600)


def _read_tree(folder: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if not folder.is_dir():
        return files
    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        files[path.relative_to(folder).as_posix()] = path.read_bytes()
    return files


def _digest(files: dict[str, bytes]) -> str:
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(files):
        if path == META:
            continue
        digest.update(path.encode("utf-8") + b"\0" + hashlib.sha256(files[path]).digest())
    return digest.hexdigest()

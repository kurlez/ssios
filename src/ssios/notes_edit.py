"""Changes to the vault from the Notes page: save, create, daily note, template, rename with link updates,
move to trash, paste an image, and bookmarks.

Rules that hold for every change:
- Only ordinary notes and attachments are written. A hidden folder such as `.obsidian` is never written.
  Obsidian's settings in `.obsidian` are read to place new notes, daily notes, templates, and attachments
  where Obsidian would.
- A save names the version it started from. If the file changed since, nothing is written and the
  caller gets `note_conflict` with the text on disk.
- A file is written beside itself under a hidden name and renamed into place, so a reader never sees half a note.
- Nothing is deleted. Trash moves a note into the vault's `.trash` folder, where Obsidian keeps deleted notes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import secrets
from datetime import datetime
from pathlib import Path

from ssios.errors import SsiosError
from ssios.util import ensure_parent_chain, read_json, reject_symlink, walk_nofollow, write_json

_BAD_NAME = re.compile(r'[*"\\/<>:|?#^\[\]\x00-\x1f]')
_ATTACH_BYTES = 25_000_000


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _root(settings, account_id: str) -> Path:
    from ssios.vault import account_vault

    root = account_vault(settings, account_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _note_rel(rel: str) -> str:
    from ssios.vault import _safe_rel

    return _safe_rel(rel, (".md",))


def obsidian_setting(settings, account_id: str, name: str) -> dict:
    """One of Obsidian's settings files, read only. Missing or unreadable is empty."""
    root = _root(settings, account_id)
    path = root / ".obsidian" / f"{name}.json"
    try:
        if path.is_symlink() or (root / ".obsidian").is_symlink() or not path.is_file() or path.stat().st_size > 400_000:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _mode(settings, root: Path) -> int:
    state = Path(settings.state_dir).resolve()
    return 0o600 if state in root.resolve().parents else 0o644


def _read_current(root: Path, rel: str) -> str | None:
    try:
        path = walk_nofollow(root, rel, expect="file")
    except SsiosError as exc:
        if exc.code == "not_found":
            return None
        raise
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        return os.read(fd, max(os.fstat(fd).st_size, 0) + 1).decode("utf-8", "replace")
    finally:
        os.close(fd)


def _atomic(root: Path, rel: str, data: bytes, mode: int) -> None:
    final = ensure_parent_chain(root, rel)
    temp = final.with_name(f".{final.name}.ssios-{secrets.token_hex(4)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, final)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def write_note(settings, account_id: str, rel: str, text: str, base: str) -> str:
    """Save a note. base is the hash the editor started from, or empty for a note that must not exist yet."""
    from ssios.notes_index import forget
    from ssios.vault import NOTE_BYTES

    rel = _note_rel(rel)
    root = _root(settings, account_id)
    current = _read_current(root, rel)
    if (current is None and base) or (current is not None and content_hash(current) != base):
        error = SsiosError("note_conflict", "This note changed on disk after you opened it. Nothing was saved.", 409)
        error.current = current or ""
        raise error
    body = str(text).replace("\r\n", "\n")
    if current is not None and current.count("\r\n") > current.count("\n") / 2:
        body = body.replace("\n", "\r\n")
    data = body.encode("utf-8")
    if len(data) > NOTE_BYTES:
        raise SsiosError("note_too_large", "A note can be at most 2 MB.", 400)
    _atomic(root, rel, data, _mode(settings, root))
    forget(settings, account_id, rel)
    return content_hash(body)


def clean_name(name: str) -> str:
    """A file name Obsidian accepts: no * " \\ / < > : | ? # ^ [ ], no leading dot, at most 120 characters."""
    text = _BAD_NAME.sub(" ", str(name or "")).strip().lstrip(".-").strip()
    text = re.sub(r"\s+", " ", text)[:120].strip()
    if text.casefold().endswith(".md"):
        text = text[:-3].strip()
    return text


def _folder(folder: str) -> str:
    text = str(folder or "").strip().strip("/")
    if not text:
        return ""
    from ssios.vault import _safe_rel

    return _safe_rel(text + "/x.md", (".md",))[:-5]


def free_path(settings, account_id: str, folder: str, name: str) -> str:
    """folder/name.md, or name 1.md, name 2.md when it is taken."""
    root = _root(settings, account_id)
    base = clean_name(name) or "Untitled"
    folder = _folder(folder)
    for number in range(0, 500):
        stem = base if number == 0 else f"{base} {number}"
        rel = f"{folder}/{stem}.md" if folder else f"{stem}.md"
        if not (root / rel).exists():
            return _note_rel(rel)
    raise SsiosError("note_rejected", "No free name was found.", 409)


def new_note_folder(settings, account_id: str, current: str) -> str:
    """Where Obsidian puts a new note: the vault root, the current note's folder, or a set folder."""
    app = obsidian_setting(settings, account_id, "app")
    where = str(app.get("newFileLocation") or "root")
    if where == "current" and "/" in current:
        return current.rsplit("/", 1)[0]
    if where == "folder":
        try:
            return _folder(str(app.get("newFileFolderPath") or ""))
        except SsiosError:
            return ""
    return ""


def create_note(settings, account_id: str, rel: str, text: str = "") -> str:
    rel = _note_rel(rel)
    write_note(settings, account_id, rel, text, "")
    return rel


_MOMENT = re.compile(r"\[[^\]]*\]|YYYY|YY|MMMM|MMM|MM|M|DDDD|DD|D|dddd|ddd|HH|H|hh|h|mm|ss|A|a|gggg|ww|w|Do")


def moment_format(when: datetime, fmt: str) -> str:
    """The subset of moment.js date formats Obsidian users write for daily notes and templates."""
    def one(found) -> str:
        token = found.group(0)
        if token.startswith("["):
            return token[1:-1]
        table = {
            "YYYY": f"{when.year:04d}", "YY": f"{when.year % 100:02d}", "MMMM": when.strftime("%B"), "MMM": when.strftime("%b"),
            "MM": f"{when.month:02d}", "M": str(when.month), "DDDD": f"{when.timetuple().tm_yday:03d}", "DD": f"{when.day:02d}",
            "D": str(when.day), "Do": str(when.day) + ("th" if 11 <= when.day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(when.day % 10, "th")),
            "dddd": when.strftime("%A"), "ddd": when.strftime("%a"), "HH": f"{when.hour:02d}", "H": str(when.hour),
            "hh": f"{(when.hour % 12) or 12:02d}", "h": str((when.hour % 12) or 12), "mm": f"{when.minute:02d}", "ss": f"{when.second:02d}",
            "A": "AM" if when.hour < 12 else "PM", "a": "am" if when.hour < 12 else "pm",
            "gggg": f"{when.isocalendar()[0]:04d}", "ww": f"{when.isocalendar()[1]:02d}", "w": str(when.isocalendar()[1]),
        }
        return table.get(token, token)

    return _MOMENT.sub(one, fmt or "YYYY-MM-DD")


def apply_template(text: str, title: str, when: datetime, date_format: str = "YYYY-MM-DD", time_format: str = "HH:mm") -> str:
    def date(found) -> str:
        return moment_format(when, found.group(1) or date_format)

    out = re.sub(r"\{\{\s*title\s*\}\}", title, text)
    out = re.sub(r"\{\{\s*date(?::([^}]*))?\s*\}\}", date, out)
    return re.sub(r"\{\{\s*time(?::([^}]*))?\s*\}\}", lambda found: moment_format(when, found.group(1) or time_format), out)


def templates(settings, account_id: str) -> list[str]:
    """Notes in the template folder named in Obsidian's Templates settings."""
    from ssios.vault import note_paths

    folder = str(obsidian_setting(settings, account_id, "templates").get("folder") or "").strip("/")
    if not folder:
        return []
    return [rel for rel in note_paths(settings, account_id) if rel.startswith(folder + "/")][:200]


def template_text(settings, account_id: str, template: str, title: str, when: datetime) -> str:
    from ssios.vault import read_note

    if template not in templates(settings, account_id):
        raise SsiosError("note_rejected", "That template is not in the template folder.", 400)
    conf = obsidian_setting(settings, account_id, "templates")
    return apply_template(read_note(settings, account_id, template), title, when, str(conf.get("dateFormat") or "YYYY-MM-DD"), str(conf.get("timeFormat") or "HH:mm"))


def daily_note(settings, account_id: str, when: datetime) -> tuple[str, bool]:
    """Today's note where Obsidian's Daily notes settings put it, created from its template the first time."""
    from ssios.vault import read_note

    conf = obsidian_setting(settings, account_id, "daily-notes")
    name = clean_name(moment_format(when, str(conf.get("format") or "YYYY-MM-DD")).replace("/", " ")) or when.strftime("%Y-%m-%d")
    try:
        folder = _folder(str(conf.get("folder") or ""))
    except SsiosError:
        folder = ""
    rel = _note_rel(f"{folder}/{name}.md" if folder else f"{name}.md")
    if (_root(settings, account_id) / rel).exists():
        return rel, False
    text = ""
    template = str(conf.get("template") or "").strip().strip("/")
    if template:
        candidate = template if template.casefold().endswith(".md") else template + ".md"
        try:
            text = apply_template(read_note(settings, account_id, candidate), name, when)
        except SsiosError:
            text = ""
    create_note(settings, account_id, rel, text)
    return rel, True


def link_settings(settings, account_id: str) -> dict:
    """How Obsidian is set to write links: shortest, relative, or absolute paths, wiki or Markdown, and whether renames update them."""
    app = obsidian_setting(settings, account_id, "app")
    fmt = str(app.get("newLinkFormat") or "shortest")
    return {
        "format": fmt if fmt in {"shortest", "relative", "absolute"} else "shortest",
        "markdown": bool(app.get("useMarkdownLinks")),
        "update": app.get("alwaysUpdateLinks") is not False,
    }


def link_path(target_rel: str, source: str, resolver, fmt: str, *, markdown: bool) -> str:
    """The path a new link to target_rel is written with, from source."""
    note = target_rel.casefold().endswith(".md")
    folder = source.rsplit("/", 1)[0] if "/" in source else ""
    if markdown:
        path = posixpath.relpath(target_rel, folder or ".") if fmt != "absolute" else target_rel
        return path.replace(" ", "%20")
    shown = target_rel[:-3] if note else target_rel
    if fmt == "absolute":
        return shown
    if fmt == "relative":
        return posixpath.relpath(shown, folder or ".")
    if resolver.unique_name(target_rel):
        return shown.rsplit("/", 1)[-1]
    return shown


def link_text(target_rel: str, source: str, resolver, link: dict, *, embed: bool = False, label: str = "") -> str:
    path = link_path(target_rel, source, resolver, link["format"], markdown=link["markdown"])
    mark = "!" if embed else ""
    if link["markdown"]:
        shown = label or target_rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return f"{mark}[{'' if embed else shown}]({path})"
    return f"{mark}[[{path}{'|' + label if label else ''}]]"


def _new_target(original: str, new_rel: str, resolver, source: str, kind: str, fmt: str = "shortest") -> str:
    stem = new_rel[:-3]
    if kind == "md":
        folder = source.rsplit("/", 1)[0] if "/" in source else ""
        relative = posixpath.relpath(new_rel, folder or ".")
        return relative.replace(" ", "%20")
    if fmt == "absolute" or "/" in original and fmt == "shortest" and not resolver.unique_name(new_rel):
        return stem
    if fmt == "relative":
        folder = source.rsplit("/", 1)[0] if "/" in source else ""
        return posixpath.relpath(stem, folder or ".")
    if "/" not in original and resolver.unique_name(new_rel):
        return stem.rsplit("/", 1)[-1]
    return stem


def rename_note(settings, account_id: str, old: str, new: str, *, update_links: bool = True) -> dict:
    """Rename or move a note and rewrite the links that point to it, as Obsidian does with Automatically update internal links."""
    from ssios.notes_index import WIKI, MDLINK, Resolver, forget, links_in, read_texts
    from ssios.vault import list_attachments, list_notes

    old = _note_rel(old)
    new = _note_rel(new)
    if old == new:
        return {"path": new, "updated": []}
    root = _root(settings, account_id)
    walk_nofollow(root, old, expect="file")
    if (root / new).exists() and old.casefold() != new.casefold():
        raise SsiosError("note_conflict", "A note with that name is already there.", 409)
    notes = list_notes(settings, account_id)
    attachments = list_attachments(settings, account_id)
    before = Resolver(notes, attachments)
    texts, _missed = read_texts(settings, account_id, [row["path"] for row in notes], budget=600)
    after_rows = [dict(row, path=new) if row["path"] == old else row for row in notes]
    after = Resolver(after_rows, attachments)
    fmt = link_settings(settings, account_id)["format"]
    edits: dict[str, str] = {}
    for source, text in texts.items():
        if not update_links and source != old:
            continue
        spans = []
        for link in links_in(text):
            if not link["target"]:
                continue
            hit = before.resolve(source, link["target"])
            if hit == old:
                spans.append(dict(link, to=new))
            elif source == old and hit and link["kind"] == "md" and not link["target"].startswith("/"):
                # A relative link inside the moved note is written again from its new folder.
                spans.append(dict(link, to=hit, keep_kind=True))
        if not spans:
            continue
        where = new if source == old else source
        out = []
        cursor = 0
        for link in spans:
            start, end = link["span"]
            piece = text[start:end]
            target = _new_target(link["target"], link["to"], after, where, link["kind"], fmt)
            fragment = ("#" + link["fragment"]) if link["fragment"] else ""
            if link["kind"] == "wiki":
                found = WIKI.fullmatch(piece)
                alias = ("|" + found.group(3)) if found and found.group(3) is not None else ""
                replaced = f"{'!' if link['embed'] else ''}[[{target}{fragment}{alias}]]"
            else:
                found = MDLINK.fullmatch(piece)
                label = found.group(2) if found else link["alias"]
                replaced = f"{'!' if link['embed'] else ''}[{label}]({target}{fragment.replace(' ', '%20')})"
            out.append(text[cursor:start])
            out.append(replaced)
            cursor = end
        out.append(text[cursor:])
        edits[source] = "".join(out)
    final = ensure_parent_chain(root, new)
    os.rename(walk_nofollow(root, old, expect="file"), final)
    forget(settings, account_id, old)
    updated, skipped = [], []
    for source, text in edits.items():
        where = new if source == old else source
        try:
            write_note(settings, account_id, where, text, content_hash(texts[source]))
            updated.append(where)
        except SsiosError:
            # Changed on disk since it was read. Its links are left as they were and reported.
            skipped.append(where)
    return {"path": new, "updated": sorted(updated), "skipped": sorted(skipped)}


def trash_note(settings, account_id: str, rel: str) -> str:
    """Move a note or attachment into the vault's .trash folder. Nothing is deleted."""
    from ssios.notes_index import forget
    from ssios.vault import _IMAGE_KINDS, _safe_rel

    rel = _safe_rel(rel, (".md", ".pdf", *_IMAGE_KINDS))
    root = _root(settings, account_id)
    source = walk_nofollow(root, rel, expect="file")
    trash = root / ".trash"
    reject_symlink(trash)
    trash.mkdir(mode=0o700, exist_ok=True)
    target = rel
    stem, dot, ext = rel.rpartition(".")
    number = 1
    while (trash / target).exists():
        target = f"{stem} {number}.{ext}" if dot else f"{rel} {number}"
        number += 1
    final = ensure_parent_chain(trash, target)
    os.rename(source, final)
    forget(settings, account_id, rel)
    return ".trash/" + target


def save_attachment(settings, account_id: str, current: str, data: bytes, when: datetime) -> str:
    """Store a pasted image where Obsidian's Files and links settings put attachments. Returns its path in the vault."""
    from ssios.vault import image_kind

    kind = image_kind(data)
    if not kind:
        raise SsiosError("attachment_rejected", "Only PNG, JPEG, GIF, and WebP images can be pasted.", 400)
    if len(data) > _ATTACH_BYTES:
        raise SsiosError("attachment_rejected", "A pasted image can be at most 25 MB.", 400)
    where = str(obsidian_setting(settings, account_id, "app").get("attachmentFolderPath") or "/")
    note_folder = current.rsplit("/", 1)[0] if "/" in current else ""
    if where in {"/", ""}:
        folder = ""
    elif where == "./":
        folder = note_folder
    elif where.startswith("./"):
        folder = posixpath.normpath(posixpath.join(note_folder, where[2:])) if note_folder else posixpath.normpath(where[2:])
    else:
        folder = where.strip("/")
    try:
        folder = _folder(folder)
    except SsiosError:
        folder = ""
    root = _root(settings, account_id)
    name = f"Pasted image {when.strftime('%Y%m%d%H%M%S')}"
    for number in range(0, 100):
        stem = name if number == 0 else f"{name} {number}"
        rel = f"{folder}/{stem}.{kind}" if folder else f"{stem}.{kind}"
        if not (root / rel).exists():
            _atomic(root, rel, data, _mode(settings, root))
            return rel
    raise SsiosError("attachment_rejected", "No free name was found.", 409)


def decode_image(value: str) -> bytes:
    text = str(value or "")
    if text.startswith("data:"):
        text = text.split(",", 1)[-1]
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise SsiosError("attachment_rejected", "The image could not be read.", 400) from exc


def bookmarks(settings, account_id: str) -> list[dict]:
    """SSIOS bookmarks, then the files bookmarked in Obsidian, which are shown and not changed."""
    own = read_json(Path(settings.state_dir) / "accounts" / account_id / "vault-bookmarks.json", {})
    rows = [{"path": str(rel), "source": "ssios"} for rel in (own.get("paths") if isinstance(own, dict) else None) or [] if isinstance(rel, str)]
    seen = {row["path"] for row in rows}

    def walk(items, depth: int = 0) -> None:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or depth > 6:
                continue
            if item.get("type") == "file" and isinstance(item.get("path"), str) and item["path"] not in seen:
                rows.append({"path": item["path"], "source": "obsidian", "title": str(item.get("title") or "")})
                seen.add(item["path"])
            if item.get("type") == "group":
                walk(item.get("items"), depth + 1)

    walk(obsidian_setting(settings, account_id, "bookmarks").get("items"))
    return rows[:300]


def toggle_bookmark(settings, account_id: str, rel: str) -> bool:
    path = Path(settings.state_dir) / "accounts" / account_id / "vault-bookmarks.json"
    own = read_json(path, {})
    paths = [str(item) for item in (own.get("paths") if isinstance(own, dict) else None) or [] if isinstance(item, str)]
    if rel in paths:
        paths.remove(rel)
        on = False
    else:
        paths.insert(0, rel)
        on = True
    write_json(path, {"paths": paths[:300]})
    return on


def link_mention(settings, account_id: str, source: str, target: str) -> bool:
    """Turn the first plain mention of target's name or alias in source into a link, as Obsidian's Link button does."""
    from ssios.notes_index import MDLINK, WIKI, Resolver, mask_code
    from ssios.vault import list_attachments, list_notes, read_note

    source = _note_rel(source)
    target = _note_rel(target)
    notes = list_notes(settings, account_id)
    resolver = Resolver(notes, list_attachments(settings, account_id))
    text = read_note(settings, account_id, source)
    stem = target[:-3].rsplit("/", 1)[-1]
    aliases = next((row.get("aliases") or [] for row in notes if row["path"] == target), [])
    masked = mask_code(text)
    masked = WIKI.sub(lambda found: " " * len(found.group(0)), masked)
    masked = MDLINK.sub(lambda found: " " * len(found.group(0)), masked)
    from ssios.vault import _split_frontmatter

    head = len(text) - len(_split_frontmatter(text)[1])
    best = None
    for name in [stem, *[str(alias) for alias in aliases]]:
        if not name:
            continue
        pattern = re.escape(name) if re.search(r"[\u3400-\u9fff]", name) else r"(?<![\w])" + re.escape(name) + r"(?![\w])"
        found = re.compile(pattern, re.I).search(masked, head)
        if found and (best is None or found.start() < best.start()):
            best = found
    if best is None:
        return False
    seen = text[best.start() : best.end()]
    path = link_path(target, source, resolver, link_settings(settings, account_id)["format"], markdown=False)
    label = "" if seen == path.rsplit("/", 1)[-1] else seen
    replaced = f"[[{path}{'|' + label if label else ''}]]"
    write_note(settings, account_id, source, text[: best.start()] + replaced + text[best.end() :], content_hash(text))
    return True

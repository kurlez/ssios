"""What the Notes page knows about the whole vault: note text, links, backlinks, search, outline, and graph.

Note text is read on demand and kept in this process only, keyed by size and time. Nothing more about a
note's text is written to disk. A vault on a cloud drive is read within a time limit. What was not read in
time is reported as incomplete, not guessed.
"""

from __future__ import annotations

import html
import os
import posixpath
import re
import threading
import time
from urllib.parse import unquote

from ssios.errors import SsiosError

_TEXT: dict[str, tuple[tuple[int, int], str]] = {}
_TEXT_LOCK = threading.Lock()
_TEXT_LIMIT = 80_000_000
_size = [0]

WIKI = re.compile(r"(!?)\[\[([^\[\]|\n]*?)(?:\|([^\[\]\n]*))?\]\]")
MDLINK = re.compile(r"(!?)\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_FENCE = re.compile(r"^ {0,3}(```+|~~~+)")
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_ATTACH = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")


def slug(text: str) -> str:
    """The anchor of a heading. The same text always gives the same anchor, in the page and in a link."""
    plain = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", str(text or ""))
    plain = re.sub(r"[*_`~=\[\]()]", "", plain).strip().casefold()
    plain = re.sub(r"\s+", "-", plain)
    plain = re.sub(r"[^\w\-㐀-鿿]", "", plain)
    return plain or "section"


def mask_code(text: str) -> str:
    """The same text with fenced and inline code blanked out, so a link shown as code is not a link. Positions are kept."""
    lines = text.split("\n")
    out = []
    marker = ""
    for line in lines:
        fence = _FENCE.match(line)
        if marker:
            out.append(" " * len(line))
            if fence and fence.group(1)[0] == marker[0] and len(fence.group(1)) >= len(marker):
                marker = ""
            continue
        if fence:
            marker = fence.group(1)
            out.append(" " * len(line))
            continue
        out.append(re.sub(r"(`+)(.+?)\1", lambda found: " " * len(found.group(0)), line))
    masked = "\n".join(out)
    return re.sub(r"%%.*?%%", lambda found: re.sub(r"[^\n]", " ", found.group(0)), masked, flags=re.S)


def read_texts(settings, account_id: str, rels: list[str], budget: float = 2.5) -> tuple[dict[str, str], int]:
    """Text of each note, from this process's cache when the file has not changed. Returns the texts and how many were skipped for time."""
    from ssios.vault import account_vault, read_note

    root = account_vault(settings, account_id)
    found: dict[str, str] = {}
    missed = 0
    deadline = time.monotonic() + budget
    for rel in rels:
        key = str(root / rel)
        try:
            info = os.lstat(root / rel)
        except OSError:
            continue
        stamp = (info.st_mtime_ns, info.st_size)
        with _TEXT_LOCK:
            hit = _TEXT.get(key)
        if hit and hit[0] == stamp:
            found[rel] = hit[1]
            continue
        if time.monotonic() > deadline:
            missed += 1
            continue
        try:
            text = read_note(settings, account_id, rel)
        except SsiosError:
            continue
        _remember(key, stamp, text)
        found[rel] = text
    return found, missed


def _remember(key: str, stamp: tuple[int, int], text: str) -> None:
    with _TEXT_LOCK:
        old = _TEXT.pop(key, None)
        if old:
            _size[0] -= len(old[1])
        while _TEXT and _size[0] + len(text) > _TEXT_LIMIT:
            _drop, gone = _TEXT.pop(next(iter(_TEXT)))
            _size[0] -= len(gone)
        _TEXT[key] = (stamp, text)
        _size[0] += len(text)


def forget(settings, account_id: str, rel: str) -> None:
    from ssios.vault import account_vault

    with _TEXT_LOCK:
        old = _TEXT.pop(str(account_vault(settings, account_id) / rel), None)
        if old:
            _size[0] -= len(old[1])


class Resolver:
    """Where a link points, the way Obsidian resolves it: a path, then the one note with that name, then an alias."""

    def __init__(self, notes: list[dict], attachments: list[dict] | None = None) -> None:
        self.paths: dict[str, str] = {}
        self.by_name: dict[str, list[str]] = {}
        self.by_alias: dict[str, str] = {}
        self.titles: dict[str, str] = {}
        for row in list(notes) + list(attachments or []):
            rel = row["path"]
            self.paths[rel.casefold()] = rel
            stem = rel[:-3] if rel.casefold().endswith(".md") else rel
            self.paths.setdefault(stem.casefold(), rel)
            name = stem.rsplit("/", 1)[-1].casefold()
            self.by_name.setdefault(name, []).append(rel)
            if rel.casefold().endswith(".md"):
                self.titles[rel] = row.get("title") or name
                for alias in row.get("aliases") or []:
                    self.by_alias.setdefault(str(alias).casefold(), rel)
        for rels in self.by_name.values():
            rels.sort(key=lambda one: (one.count("/"), len(one), one))

    def resolve(self, current: str, target: str) -> str:
        text = unquote(str(target or "")).strip().replace("\\", "/")
        if not text:
            return current
        if text.startswith(("http://", "https://", "mailto:")) or "\x00" in text:
            return ""
        folder = current.rsplit("/", 1)[0] if "/" in current else ""
        tries = []
        if text.startswith("/"):
            tries.append(text.lstrip("/"))
        else:
            tries.append(posixpath.normpath(posixpath.join(folder, text)) if folder else posixpath.normpath(text))
            tries.append(posixpath.normpath(text))
        for candidate in tries:
            if candidate.startswith("..") or candidate.startswith("/"):
                continue
            for form in (candidate, candidate + ".md"):
                hit = self.paths.get(form.casefold())
                if hit:
                    return hit
        name = text.rsplit("/", 1)[-1]
        key = (name[:-3] if name.casefold().endswith(".md") else name).casefold()
        rels = self.by_name.get(key) or []
        if "/" in text:
            tail = (text if not text.casefold().endswith(".md") else text[:-3]).casefold()
            rels = [rel for rel in rels if (rel[:-3] if rel.casefold().endswith(".md") else rel).casefold().endswith(tail)]
        if rels:
            return rels[0]
        return self.by_alias.get(text.casefold(), "")

    def unique_name(self, rel: str) -> bool:
        stem = rel[:-3] if rel.casefold().endswith(".md") else rel
        return len(self.by_name.get(stem.rsplit("/", 1)[-1].casefold(), [])) <= 1


def links_in(text: str) -> list[dict]:
    """Links in a note: target, heading or block, alias, whether it is an embed, the span, and the kind."""
    body = mask_code(text)
    found = []
    for match in WIKI.finditer(body):
        target, _sep, fragment = match.group(2).partition("#")
        found.append({"target": target.strip(), "fragment": fragment.strip(), "alias": (match.group(3) or "").strip(), "embed": match.group(1) == "!", "span": match.span(), "kind": "wiki"})
    for match in MDLINK.finditer(body):
        url = match.group(3)
        if url.startswith(("http://", "https://", "mailto:", "data:", "javascript:")):
            continue
        target, _sep, fragment = unquote(url).partition("#")
        if not target and not fragment:
            continue
        found.append({"target": target.strip(), "fragment": fragment.strip(), "alias": match.group(2), "embed": match.group(1) == "!", "span": match.span(), "kind": "md"})
    found.sort(key=lambda row: row["span"][0])
    return found


def _line_at(text: str, at: int) -> str:
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    line = text[start : end if end >= 0 else len(text)].strip()
    return line[:220]


def outline(text: str) -> list[dict]:
    from ssios.vault import _split_frontmatter

    _meta, body = _split_frontmatter(text)
    masked = mask_code(body).split("\n")
    seen: dict[str, int] = {}
    rows = []
    for line in masked:
        found = _HEADING.match(line)
        if not found:
            continue
        title = found.group(2)
        anchor = slug(title)
        count = seen.get(anchor, 0)
        seen[anchor] = count + 1
        rows.append({"level": len(found.group(1)), "text": re.sub(r"[*_`~=]|\[\[([^\]|]*\|)?|\]\]", "", title), "anchor": anchor if count == 0 else f"{anchor}-{count}"})
    return rows


def section(text: str, fragment: str) -> str:
    """The part of a note under one heading, or the paragraph that ends with ^block. Empty when it is not there."""
    from ssios.vault import _split_frontmatter

    _meta, body = _split_frontmatter(text)
    lines = body.split("\n")
    if fragment.startswith("^"):
        mark = fragment[1:]
        for index, line in enumerate(lines):
            if re.search(r"(?:^|\s)\^" + re.escape(mark) + r"\s*$", line):
                start = index
                while start > 0 and lines[start - 1].strip() and not _HEADING.match(lines[start - 1]):
                    start -= 1
                return "\n".join(lines[start : index + 1])
        return ""
    wanted = slug(fragment)
    masked = mask_code(body).split("\n")
    for index, line in enumerate(masked):
        found = _HEADING.match(line)
        if found and slug(found.group(2)) == wanted:
            level = len(found.group(1))
            end = index + 1
            while end < len(lines):
                other = _HEADING.match(masked[end])
                if other and len(other.group(1)) <= level:
                    break
                end += 1
            return "\n".join(lines[index:end])
    return ""


class VaultView:
    """One reading of the vault for one request: the list, the resolver, and the text that could be read in time."""

    def __init__(self, settings, account_id: str, budget: float = 2.5) -> None:
        from ssios.vault import list_attachments, list_notes

        self.settings = settings
        self.account_id = account_id
        self.notes = list_notes(settings, account_id)
        self.attachments = list_attachments(settings, account_id)
        self.resolver = Resolver(self.notes, self.attachments)
        self.budget = budget
        self._texts: dict[str, str] | None = None
        self.missed = 0

    def texts(self) -> dict[str, str]:
        if self._texts is None:
            self._texts, self.missed = read_texts(self.settings, self.account_id, [row["path"] for row in self.notes], self.budget)
        return self._texts

    def title(self, rel: str) -> str:
        return self.resolver.titles.get(rel) or rel.rsplit("/", 1)[-1]

    def backlinks(self, rel: str) -> list[dict]:
        rows = []
        for source, text in self.texts().items():
            contexts = []
            for link in links_in(text):
                if self.resolver.resolve(source, link["target"]) == rel and not (source == rel and not link["target"]):
                    contexts.append(_line_at(text, link["span"][0]))
            if contexts:
                rows.append({"path": source, "title": self.title(source), "contexts": contexts[:3], "count": len(contexts)})
        rows.sort(key=lambda row: row["title"].casefold())
        return rows

    def unlinked(self, rel: str, linked: set[str]) -> list[dict]:
        """Notes that name this note in plain text without linking to it."""
        stem = rel[:-3].rsplit("/", 1)[-1] if rel.casefold().endswith(".md") else ""
        names = [name for name in [stem, *self._aliases(rel)] if name]
        patterns = []
        for name in names:
            if re.search(r"[㐀-鿿]", name):
                if len(name) >= 2:
                    patterns.append(re.compile(re.escape(name), re.I))
            elif len(name) >= 3:
                patterns.append(re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])", re.I))
        if not patterns:
            return []
        rows = []
        for source, text in self.texts().items():
            if source == rel or source in linked:
                continue
            masked = WIKI.sub(lambda found: " " * len(found.group(0)), mask_code(text))
            masked = MDLINK.sub(lambda found: " " * len(found.group(0)), masked)
            for pattern in patterns:
                hit = pattern.search(masked)
                if hit:
                    rows.append({"path": source, "title": self.title(source), "contexts": [_line_at(text, hit.start())]})
                    break
            if len(rows) >= 30:
                break
        return rows

    def _aliases(self, rel: str) -> list[str]:
        for row in self.notes:
            if row["path"] == rel:
                return [str(alias) for alias in row.get("aliases") or []]
        return []

    def outgoing(self, rel: str, text: str) -> list[dict]:
        rows: dict[str, dict] = {}
        for link in links_in(text):
            if not link["target"]:
                continue
            hit = self.resolver.resolve(rel, link["target"])
            key = hit or "missing:" + link["target"].casefold()
            if key not in rows:
                rows[key] = {"path": hit, "target": link["target"], "title": self.title(hit) if hit else link["target"], "embed": link["embed"]}
        return sorted(rows.values(), key=lambda row: (not row["path"], row["title"].casefold()))

    def graph(self, limit: int = 600) -> dict:
        texts = self.texts()
        edges: set[tuple[str, str]] = set()
        degree: dict[str, int] = {}
        for source, text in texts.items():
            for link in links_in(text):
                hit = self.resolver.resolve(source, link["target"]) if link["target"] else ""
                if hit and hit != source and hit.casefold().endswith(".md"):
                    pair = (source, hit)
                    if pair not in edges:
                        edges.add(pair)
                        degree[source] = degree.get(source, 0) + 1
                        degree[hit] = degree.get(hit, 0) + 1
        chosen = sorted((row["path"] for row in self.notes), key=lambda rel: -degree.get(rel, 0))[:limit]
        index = {rel: number for number, rel in enumerate(chosen)}
        return {
            "nodes": [{"path": rel, "title": self.title(rel), "degree": degree.get(rel, 0)} for rel in chosen],
            "edges": [[index[a], index[b]] for a, b in edges if a in index and b in index],
            "missed": self.missed,
        }

    def search(self, query: str, limit: int = 60) -> dict:
        terms = parse_query(query)
        if not any(terms.values()):
            return {"results": [], "missed": 0, "terms": terms}
        texts = self.texts() if terms["words"] or terms["phrases"] or terms["not"] else {}
        rows = []
        for note in self.notes:
            rel = note["path"]
            title = str(note.get("title") or "")
            folded_path = rel.casefold()
            tags = [str(tag).casefold() for tag in note.get("tags") or []]
            if any(not any(tag == want or tag.startswith(want + "/") for tag in tags) for want in terms["tag"]):
                continue
            if any(want not in folded_path for want in terms["path"]):
                continue
            if any(want not in rel.rsplit("/", 1)[-1].casefold() for want in terms["file"]):
                continue
            text = texts.get(rel)
            needles = terms["words"] + terms["phrases"]
            head = (title + " " + rel).casefold()
            body = text.casefold() if text is not None else ""
            if any(word in head or word in body for word in terms["not"]):
                continue
            if needles and not all(needle in head or needle in body for needle in needles):
                continue
            score = sum(5 for needle in needles if needle in title.casefold()) + sum(2 for needle in needles if needle in folded_path)
            score += sum(min(body.count(needle), 10) for needle in needles)
            snippets = _snippets(text, needles) if text is not None and needles else []
            rows.append({"path": rel, "title": title or rel, "score": score, "snippets": snippets, "hits": sum(body.count(needle) for needle in needles)})
        rows.sort(key=lambda row: (-row["score"], row["title"].casefold()))
        return {"results": rows[:limit], "total": len(rows), "missed": self.missed if texts else 0, "terms": terms}


def parse_query(query: str) -> dict:
    """Words, "phrases", -excluded words, and tag: path: file: filters, as Obsidian's search reads them."""
    terms: dict[str, list[str]] = {"words": [], "phrases": [], "not": [], "tag": [], "path": [], "file": []}
    for match in re.finditer(r'(-)?(?:(tag|path|file):)?(?:"([^"]+)"|(\S+))', str(query or "")[:300]):
        negate, field, phrase, word = match.groups()
        value = (phrase or word or "").strip().casefold()
        if not value:
            continue
        if field == "tag":
            terms["tag"].append(value.lstrip("#"))
        elif field in {"path", "file"}:
            terms[field].append(value)
        elif negate:
            terms["not"].append(value)
        elif phrase:
            terms["phrases"].append(value)
        elif value.startswith("#") and len(value) > 1:
            terms["tag"].append(value[1:])
        else:
            terms["words"].append(value)
    return terms


def _snippets(text: str, needles: list[str], count: int = 3) -> list[str]:
    from ssios.vault import _split_frontmatter

    _meta, body = _split_frontmatter(text)
    folded = body.casefold()
    found = []
    taken: list[int] = []
    for needle in needles:
        start = 0
        while len(found) < count:
            at = folded.find(needle, start)
            if at < 0:
                break
            start = at + len(needle)
            if any(abs(at - other) < 80 for other in taken):
                continue
            taken.append(at)
            left = max(0, at - 70)
            right = min(len(body), at + len(needle) + 90)
            piece = " ".join(body[left:right].split())
            found.append(("…" if left else "") + _mark(piece, needles) + ("…" if right < len(body) else ""))
    return found


def _mark(piece: str, needles: list[str]) -> str:
    pattern = re.compile("|".join(re.escape(needle) for needle in sorted(needles, key=len, reverse=True)), re.I)
    out = []
    cursor = 0
    for hit in pattern.finditer(piece):
        out.append(html.escape(piece[cursor : hit.start()]))
        out.append("<mark>" + html.escape(hit.group(0)) + "</mark>")
        cursor = hit.end()
    out.append(html.escape(piece[cursor:]))
    return "".join(out)

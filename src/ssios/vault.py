"""Per-account Obsidian vault. Clips are local writes. The guest never sees them."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from ssios.errors import SsiosError
from ssios.util import read_nofollow, write_nofollow

_BLOCKED = {".ssios", ".ssios-demo", ".ssios-memfs"}
_IMAGE_LIMIT = 6
_IMAGE_BYTES = 400_000
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def vault_base(settings) -> Path:
    raw = str(getattr(settings, "skills_vault_dir", "") or "")
    state = Path(settings.state_dir).resolve()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = state / path
    else:
        path = state / "vaults"
    resolved = path.resolve()
    if any(part in _BLOCKED for part in resolved.parts):
        raise SsiosError("path_rejected", "vault path is not allowed", 400)
    if resolved == state or (state in resolved.parents and resolved.relative_to(state).parts[:1] and resolved.relative_to(state).parts[0] in {"accounts", "audit", "memory", "secrets"}):
        raise SsiosError("path_rejected", "vault path is not allowed", 400)
    return resolved


def account_vault(settings, account_id: str) -> Path:
    if not re.fullmatch(r"acct-[0-9a-f]{16}", account_id):
        raise SsiosError("path_rejected", "account id rejected", 400)
    return vault_base(settings) / account_id


def slugify(title: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(title or "").casefold()).strip("-")
    return (text or "clip")[:60]


def html_to_markdown(raw: bytes, page_url: str) -> dict:
    parser = _Convert(page_url)
    try:
        parser.feed(raw.decode("utf-8", "replace"))
        parser.close()
    except Exception:
        parser.parts.append(re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace")))
    title = " ".join(parser.title).strip() or "Untitled"
    body = "\n\n".join(part.strip() for part in parser.parts if part.strip())
    return {
        "title": title[:180],
        "author": parser.author[:120],
        "published": parser.published[:40],
        "body": body[:20000],
        "images": parser.images[:_IMAGE_LIMIT],
    }


def render_markdown(markdown: str, *, link_for) -> tuple[dict, str]:
    """Return frontmatter properties and sanitized HTML. link_for(url) may rewrite a relative or wiki target."""
    meta, body = _split_frontmatter(markdown)
    html_body = _md_html(body, link_for)
    return meta, html_body


def list_notes(settings, account_id: str) -> list[dict]:
    root = account_vault(settings, account_id)
    folder = root / "Clippings"
    if not folder.is_dir():
        return []
    notes = []
    for path in sorted(folder.glob("*.md")):
        if path.is_symlink():
            continue
        rel = "Clippings/" + path.name
        try:
            text = read_nofollow(root, rel, limit=200_000).decode("utf-8", "replace")
        except SsiosError:
            continue
        meta, _body = _split_frontmatter(text)
        notes.append({"path": rel, "title": str(meta.get("title") or path.stem), "source": str(meta.get("source") or "")})
    return notes


def read_note(settings, account_id: str, rel: str) -> str:
    if not rel.startswith("Clippings/") or rel.count("..") or not rel.endswith(".md"):
        raise SsiosError("path_rejected", "note path rejected", 400)
    return read_nofollow(account_vault(settings, account_id), rel, limit=200_000).decode("utf-8", "replace")


def read_asset(settings, account_id: str, rel: str) -> tuple[bytes, str]:
    if not rel.startswith("Clippings/assets/") or ".." in rel.split("/"):
        raise SsiosError("path_rejected", "asset path rejected", 400)
    suffix = Path(rel).suffix.lower()
    kinds = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
    if suffix not in kinds:
        raise SsiosError("path_rejected", "asset type rejected", 400)
    data = read_nofollow(account_vault(settings, account_id), rel, limit=_IMAGE_BYTES)
    if _image_ext(data) != suffix.lstrip("."):
        raise SsiosError("path_rejected", "asset type rejected", 400)
    return data, kinds[suffix]


class _Convert(HTMLParser):
    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.skip = 0
        self.title: list[str] = []
        self.in_title = False
        self.parts: list[str] = []
        self.inline: list[str] = []
        self.images: list[tuple[str, str]] = []
        self.author = ""
        self.published = ""
        self.href = ""
        self.in_link = False
        self.link_text: list[str] = []
        self.in_pre = False
        self.code: list[str] = []
        self.in_code = False
        self.list_kind: list[str] = []
        self.heading = 0
        self.in_cell = False
        self.cell: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []
        self.in_table = False

    def handle_starttag(self, tag, attrs):
        attr = {key: value or "" for key, value in attrs}
        if tag in {"script", "style", "noscript", "template"}:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "meta":
            key = (attr.get("name") or attr.get("property") or "").lower()
            if key in {"author", "article:author"} and not self.author:
                self.author = attr.get("content") or ""
            if key in {"article:published_time", "date"} and not self.published:
                self.published = attr.get("content") or ""
        if tag == "title":
            self.in_title = True
        if tag in {"h1", "h2", "h3", "h4"}:
            self._flush()
            self.heading = int(tag[1])
        if tag == "p":
            self._flush()
        if tag == "ul":
            self.list_kind.append("ul")
        if tag == "ol":
            self.list_kind.append("ol")
        if tag == "li":
            self._flush()
        if tag == "a":
            self.in_link = True
            self.href = attr.get("href") or ""
            self.link_text = []
        if tag == "img" and len(self.images) < _IMAGE_LIMIT:
            self.images.append((attr.get("src") or "", attr.get("alt") or "image"))
            self.inline.append(f"[[IMG:{len(self.images) - 1}]]")
        if tag == "pre":
            self.in_pre = True
            self.code = []
        elif tag == "code" and not self.in_pre:
            self.in_code = True
        if tag == "table":
            self._flush()
            self.in_table = True
            self.rows = []
        if tag == "tr":
            self.row = []
        if tag in {"td", "th"}:
            self.in_cell = True
            self.cell = []
        if tag == "br":
            self.inline.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"} and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = False
        if tag in {"h1", "h2", "h3", "h4"} and self.heading:
            text = self._inline()
            self.parts.append("#" * self.heading + " " + text)
            self.heading = 0
        if tag == "p":
            text = self._inline()
            if text:
                self.parts.append(text)
        if tag == "li":
            text = self._inline()
            bullet = "1." if self.list_kind and self.list_kind[-1] == "ol" else "-"
            self.parts.append(f"{bullet} {text}")
        if tag in {"ul", "ol"} and self.list_kind:
            self.list_kind.pop()
        if tag == "a" and self.in_link:
            label = " ".join(self.link_text).strip() or self.href
            href = self.href
            if href.startswith("http://") or href.startswith("https://"):
                self.inline.append(f"[{label}]({href})")
            self.in_link = False
            self.href = ""
        if tag == "pre":
            self.parts.append("```\n" + "".join(self.code).strip("\n") + "\n```")
            self.in_pre = False
        if tag == "code" and not self.in_pre:
            self.in_code = False
        if tag in {"td", "th"} and self.in_cell:
            self.row.append(" ".join(self.cell).strip().replace("|", "\\|"))
            self.in_cell = False
        if tag == "tr" and self.in_table:
            if self.row:
                self.rows.append(self.row)
            self.row = []
        if tag == "table" and self.in_table:
            self.parts.append(_table(self.rows))
            self.in_table = False
            self.rows = []

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title.append(data)
            return
        if self.in_pre:
            self.code.append(data)
            return
        if self.in_cell:
            self.cell.append(data.strip())
            return
        text = " ".join(data.split())
        if not text:
            return
        if self.in_code:
            text = f"`{text}`"
        if self.in_link:
            self.link_text.append(text)
            return
        self.inline.append(text)

    def _inline(self) -> str:
        text = " ".join(self.inline).strip()
        self.inline = []
        return text

    def _flush(self) -> None:
        text = self._inline()
        if text:
            self.parts.append(text)


def _table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    head = "| " + " | ".join(padded[0]) + " |"
    bar = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = ["| " + " | ".join(row) + " |" for row in padded[1:]]
    return "\n".join([head, bar, *body])


def _split_frontmatter(markdown: str) -> tuple[dict, str]:
    if not markdown.startswith("---\n"):
        return {}, markdown
    end = markdown.find("\n---\n", 4)
    if end < 0:
        return {}, markdown
    raw = markdown[4:end]
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, markdown[end + 5 :]


def _md_html(body: str, link_for) -> str:
    import html as html_lib

    chunks = []
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            code = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            chunks.append("<pre><code>" + html_lib.escape("\n".join(code)) + "</code></pre>")
            index += 1
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            level = min(max(level, 1), 4)
            chunks.append(f"<h{level}>" + _inline(line[level:].strip(), link_for) + f"</h{level}>")
        elif line.startswith("| ") and index + 1 < len(lines) and lines[index + 1].startswith("|"):
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip("|").split("|")]
                rows.append(cells)
                index += 1
            table = ["<table>"]
            for row_index, cells in enumerate(rows):
                if row_index == 1 and all(set(cell) <= set("-: ") for cell in cells):
                    continue
                tag = "th" if row_index == 0 else "td"
                table.append("<tr>" + "".join(f"<{tag}>{html_lib.escape(cell)}</{tag}>" for cell in cells) + "</tr>")
            table.append("</table>")
            chunks.append("".join(table))
            continue
        elif line.startswith("- ") or line.startswith("1. "):
            items = []
            ordered = line.startswith("1. ")
            while index < len(lines) and (lines[index].startswith("- ") or lines[index].startswith("1. ")):
                items.append(_inline(lines[index].split(" ", 1)[1], link_for))
                index += 1
            tag = "ol" if ordered else "ul"
            chunks.append(f"<{tag}>" + "".join(f"<li>{item}</li>" for item in items) + f"</{tag}>")
            continue
        elif line.strip():
            chunks.append("<p>" + _inline(line, link_for) + "</p>")
        index += 1
    return "\n".join(chunks)


def _inline(text: str, link_for) -> str:
    import html as html_lib

    text = re.sub(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", lambda match: f"![]({match.group(1).strip()})", text)
    text = re.sub(
        r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
        lambda match: f"[{(match.group(2) or match.group(1)).strip()}]({match.group(1).strip()})",
        text,
    )
    parts = []
    cursor = 0
    pattern = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)|\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`")
    for match in pattern.finditer(text):
        parts.append(html_lib.escape(text[cursor:match.start()]))
        if match.group(1) is not None or match.group(0).startswith("!["):
            alt = html_lib.escape(match.group(1) or "")
            href = link_for(match.group(2))
            if href:
                parts.append(f'<img alt="{alt}" src="{html_lib.escape(href, quote=True)}">')
            else:
                parts.append(alt)
        elif match.group(3) is not None:
            label = html_lib.escape(match.group(3))
            href = link_for(match.group(4))
            if href.startswith("https://") or href.startswith("http://") or href.startswith("/ui/vault/"):
                parts.append(f'<a href="{html_lib.escape(href, quote=True)}" rel="noopener noreferrer">{label}</a>')
            else:
                parts.append(label)
        else:
            parts.append("<code>" + html_lib.escape(match.group(5) or "") + "</code>")
        cursor = match.end()
    parts.append(html_lib.escape(text[cursor:]))
    return "".join(parts)


def image_kind(data: bytes) -> str:
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return ""


def _image_ext(data: bytes) -> str:
    kind = image_kind(data)
    return "jpg" if kind == "jpg" else kind

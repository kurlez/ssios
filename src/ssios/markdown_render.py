"""Markdown to safe HTML for notes, in the dialect Obsidian writes.

Blocks: headings, rules, fenced code, quotes and callouts, nested lists, task lists, tables, paragraphs.
Inline: bold, italic, strikethrough, highlight, code, links, images, wiki links, bare addresses, tags.

Every piece of note text is escaped. HTML inside a note is shown as text, not run. A link is kept
only when link_for returns an http(s) address or a path on this site's Notes pages.
"""

from __future__ import annotations

import html
import re

_FENCE = re.compile(r"^ {0,3}(```+|~~~+)\s*([\w+#.-]*)\s*$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])(\s+|$)(.*)$")
_TABLE_BAR = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_CALLOUT = re.compile(r"^\[!([A-Za-z-]+)\][+-]?\s*(.*)$")
_SAFE_PREFIX = ("https://", "http://", "/ui/vault/")
_TRAIL = ".,;:!?。，、；：！？”’\"'"


def render(body: str, link_for) -> str:
    lines = [line.replace("\t", "    ") for line in str(body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return _blocks(lines, link_for, tight=False, depth=0)


def _blocks(lines: list[str], link_for, *, tight: bool, depth: int) -> str:
    out: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            code = []
            index += 1
            while index < total and not re.match(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$", lines[index]):
                code.append(lines[index])
                index += 1
            index += 1
            lang = fence.group(2)
            attr = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
            out.append(f"<pre><code{attr}>" + html.escape("\n".join(code)) + "</code></pre>")
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>" + inline(heading.group(2), link_for) + f"</h{level}>")
            index += 1
            continue
        if _RULE.match(line):
            out.append("<hr>")
            index += 1
            continue
        if line.lstrip().startswith(">"):
            quote = []
            while index < total and lines[index].lstrip().startswith(">"):
                quote.append(re.sub(r"^\s*> ?", "", lines[index]))
                index += 1
            out.append(_quote(quote, link_for, depth))
            continue
        if "|" in line and index + 1 < total and _TABLE_BAR.match(lines[index + 1]) and "-" in lines[index + 1]:
            rows = [line, lines[index + 1]]
            index += 2
            while index < total and "|" in lines[index] and lines[index].strip():
                rows.append(lines[index])
                index += 1
            out.append(_table(rows, link_for))
            continue
        if _ITEM.match(line) and depth < 12:
            block = []
            while index < total:
                current = lines[index]
                if not current.strip():
                    # A blank line stays in the list only when the list goes on after it.
                    ahead = index + 1
                    while ahead < total and not lines[ahead].strip():
                        ahead += 1
                    if ahead < total and (_ITEM.match(lines[ahead]) or lines[ahead].startswith(" ")):
                        block.append("")
                        index += 1
                        continue
                    break
                plain = not current.startswith(" ")
                if plain and (_RULE.match(current) or _HEADING.match(current) or _FENCE.match(current) or current.startswith(">")):
                    break
                if plain and not _ITEM.match(current) and block and not block[-1].strip():
                    break
                block.append(current)
                index += 1
            out.append(_list(block, link_for, depth))
            continue
        para = [line.strip()]
        index += 1
        while index < total and lines[index].strip() and not _starts_block(lines, index):
            para.append(lines[index].strip())
            index += 1
        text = "<br>".join(inline(part, link_for) for part in para)
        # In a tight list the item's first line is not wrapped, so the bullet and the text share a line.
        out.append(text if tight and not out else f"<p>{text}</p>")
    return "\n".join(out)


def _starts_block(lines: list[str], index: int) -> bool:
    line = lines[index]
    if _FENCE.match(line) or _HEADING.match(line) or line.lstrip().startswith(">") or _ITEM.match(line):
        return True
    if _RULE.match(line):
        return True
    return "|" in line and index + 1 < len(lines) and bool(_TABLE_BAR.match(lines[index + 1])) and "-" in lines[index + 1]


def _quote(lines: list[str], link_for, depth: int) -> str:
    callout = _CALLOUT.match(lines[0].strip()) if lines else None
    if callout:
        kind = callout.group(1).casefold()
        title = callout.group(2).strip() or kind.capitalize()
        inner = _blocks(lines[1:], link_for, tight=False, depth=depth + 1)
        return f'<div class="callout callout-{html.escape(kind, quote=True)}"><div class="callout-title">{inline(title, link_for)}</div>{inner}</div>'
    return "<blockquote>" + _blocks(lines, link_for, tight=False, depth=depth + 1) + "</blockquote>"


def _cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells, current, code = [], "", False
    position = 0
    while position < len(text):
        char = text[position]
        if char == "\\" and position + 1 < len(text) and text[position + 1] == "|":
            current += "|"
            position += 2
            continue
        if char == "`":
            code = not code
        if char == "|" and not code:
            cells.append(current.strip())
            current = ""
        else:
            current += char
        position += 1
    cells.append(current.strip())
    return cells


def _table(rows: list[str], link_for) -> str:
    head = _cells(rows[0])
    aligns = []
    for cell in _cells(rows[1]):
        left, right = cell.startswith(":"), cell.endswith(":")
        aligns.append("center" if left and right else "right" if right else "left" if left else "")

    def row(cells: list[str], tag: str) -> str:
        parts = []
        for position in range(len(head)):
            value = cells[position] if position < len(cells) else ""
            align = aligns[position] if position < len(aligns) else ""
            style = f' style="text-align:{align}"' if align else ""
            parts.append(f"<{tag}{style}>{inline(value, link_for)}</{tag}>")
        return "<tr>" + "".join(parts) + "</tr>"

    body = "".join(row(_cells(line), "td") for line in rows[2:])
    return '<div class="table-wrap"><table><thead>' + row(head, "th") + "</thead><tbody>" + body + "</tbody></table></div>"


def _list(lines: list[str], link_for, depth: int) -> str:
    """One or more lists. A change from bullets to numbers, or back, at the same level starts a new list."""
    base = len(_ITEM.match(lines[0]).group(1))
    runs: list[tuple[bool, str, list[list[str]]]] = []
    for line in lines:
        found = _ITEM.match(line)
        if found and len(found.group(1)) <= base + 1:
            ordered = found.group(2)[0].isdigit()
            if not runs or runs[-1][0] != ordered:
                runs.append((ordered, found.group(2), []))
            indent = len(found.group(1)) + len(found.group(2)) + max(1, min(len(found.group(3)), 4))
            runs[-1][2].append([str(indent), found.group(4)])
        elif runs:
            runs[-1][2][-1].append(line)
    out = []
    for ordered, marker, items in runs:
        parts = []
        for item in items:
            indent = int(item[0])
            body = [item[1]]
            for line in item[2:]:
                stripped = len(line) - len(line.lstrip(" "))
                body.append(line[min(stripped, indent):])
            while body and not body[-1].strip():
                body.pop()
            check = ""
            task = re.match(r"^\[([ xX])\]\s+(.*)$", body[0]) if body else None
            if task:
                done = task.group(1) != " "
                check = f'<input type="checkbox" disabled{" checked" if done else ""}> '
                body[0] = task.group(2)
            loose = any(not line.strip() for line in body)
            inner = _blocks(body, link_for, tight=not loose, depth=depth + 1)
            css = ' class="task"' if task else ""
            parts.append(f"<li{css}>{check}{inner}</li>")
        if ordered:
            start = int(re.match(r"\d+", marker).group(0))
            attr = f' start="{start}"' if start != 1 else ""
            out.append(f"<ol{attr}>" + "".join(parts) + "</ol>")
        else:
            out.append("<ul>" + "".join(parts) + "</ul>")
    return "\n".join(out)


_LINKISH = re.compile(
    r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)"  # image
    r"|\[([^\]]+)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)"  # link
    r"|<(https?://[^\s<>]+)>"  # <address>
    r"|(?<![\w/\"'=(])(https?://[^\s<>\u3000-\u303f\uff00-\uffef\u4e00-\u9fff]+)"  # bare address, up to Chinese text or full-width punctuation
)


def inline(text: str, link_for) -> str:
    held: list[str] = []

    def hold(value: str) -> str:
        held.append(value)
        return f"\x00{len(held) - 1}\x00"

    text = str(text or "").replace("\x00", "")
    # A backslash keeps the next mark as a plain character.
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|~=<>])", lambda found: hold(html.escape(found.group(1))), text)
    text = re.sub(r"(`+)(.+?)\1", lambda found: hold("<code>" + html.escape(found.group(2).strip()) + "</code>"), text)
    text = re.sub(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", lambda found: f"![]({found.group(1).strip().replace(' ', '%20')})", text)
    text = re.sub(
        r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
        lambda found: f"[{(found.group(2) or found.group(1)).strip()}]({found.group(1).strip().replace(' ', '%20')})",
        text,
    )

    def linkish(found) -> str:
        if found.group(2) is not None:
            alt = html.escape(found.group(1) or "")
            href = link_for(found.group(2).replace("%20", " "))
            if href and href.startswith(_SAFE_PREFIX):
                return hold(f'<img alt="{alt}" src="{html.escape(href, quote=True)}" loading="lazy">')
            return hold(alt)
        if found.group(4) is not None:
            label = _emphasis(html.escape(found.group(3)))
            href = link_for(found.group(4).replace("%20", " "))
            if href and href.startswith(_SAFE_PREFIX):
                return hold(f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer">{label}</a>')
            return hold(label)
        raw = found.group(5) or found.group(6) or ""
        tail = ""
        if found.group(6):
            while raw and (raw[-1] in _TRAIL or raw[-1] == "）" or (raw[-1] == ")" and raw.count("(") < raw.count(")"))):
                tail = raw[-1] + tail
                raw = raw[:-1]
        if not raw.startswith(("https://", "http://")):
            return found.group(0)
        shown = html.escape(raw)
        return hold(f'<a href="{html.escape(raw, quote=True)}" rel="noopener noreferrer">{shown}</a>') + tail

    text = _LINKISH.sub(linkish, text)
    text = _emphasis(html.escape(text))
    text = re.sub(r"(?<![\w&#/])#([A-Za-z一-鿿][\w/\-一-鿿]*)", r'<span class="tag">#\1</span>', text)
    return re.sub(r"\x00(\d+)\x00", lambda found: held[int(found.group(1))], text)


def _emphasis(escaped: str) -> str:
    """Bold, italic, strikethrough, and highlight on text that is already escaped."""
    text = re.sub(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", r"<strong><em>\1</em></strong>", escaped)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", r"<em>\1</em>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", text)
    text = re.sub(r"==(?=\S)(.+?)(?<=\S)==", r"<mark>\1</mark>", text)
    return text

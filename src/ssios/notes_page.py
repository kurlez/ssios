"""The Notes page: an Obsidian vault in three panes.

Left: Files (a folder tree), Recent, Search, Tags, and Bookmarks. Middle: the note, read or edited.
Right: Outline, Backlinks with unlinked mentions, Outgoing links, and a local graph. A whole-vault
graph and a quick switcher (Ctrl or Cmd + O) are one key away.
"""

from __future__ import annotations

import html
import json
import math
from datetime import datetime
from urllib.parse import quote

from ssios.errors import SsiosError

_VIEWS = ("files", "recent", "search", "tags", "bookmarks", "graph")
_GROUPS = ("Today", "Yesterday", "Previous 7 days", "Previous 30 days", "Older")
_TOOL_HINT = "A clipped or written note counts as skimmed. Mark it read after reading it with care. Chat says which one it is when it uses the note."


def _q(value: str) -> str:
    return quote(value, safe="")


def note_href(rel: str, *, view: str = "", tag: str = "", query: str = "", mode: str = "", anchor: str = "") -> str:
    href = "/ui/vault/note?path=" + _q(rel)
    if view and view != "files":
        href += "&view=" + view
    if tag:
        href += "&tag=" + _q(tag)
    if query:
        href += "&q=" + _q(query)
    if mode:
        href += "&mode=" + mode
    return href + ("#" + anchor if anchor else "")


def _anchor(fragment: str) -> str:
    from ssios.notes_index import slug

    if not fragment:
        return ""
    return "block-" + fragment[1:] if fragment.startswith("^") else slug(fragment)


class Renderer:
    """Renders one note with links resolved from that note's folder, and notes placed in it drawn inline."""

    def __init__(self, plane, account_id: str, view) -> None:
        self.plane = plane
        self.account_id = account_id
        self.view = view

    def link_for(self, current: str):
        from ssios.notes_edit import clean_name, new_note_folder

        def resolve(url: str) -> str:
            text = str(url or "")
            if text.startswith(("javascript:", "data:", "vbscript:")):
                return ""
            if text.startswith(("https://", "http://")):
                return text
            target, _sep, fragment = text.partition("#")
            hit = self.view.resolver.resolve(current, target.replace("%20", " ")) if target or fragment else ""
            if hit:
                if hit.casefold().endswith((".md", ".pdf")):
                    return note_href(hit, anchor=_anchor(fragment) if hit.casefold().endswith(".md") else "")
                return "/ui/vault/asset?path=" + _q(hit)
            name = target.replace("%20", " ").strip()
            if not name or name.casefold().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")):
                return ""
            parts = [clean_name(part) for part in name.split("/")]
            if any(not part for part in parts):
                return ""
            if len(parts) > 1:
                rel = "/".join(parts) + ".md"
            else:
                folder = new_note_folder(self.plane.settings, self.account_id, current)
                rel = f"{folder}/{parts[0]}.md" if folder else f"{parts[0]}.md"
            return note_href(rel) + "&missing=1"

        return resolve

    def embed_for(self, current: str, depth: int, stack: tuple):
        from ssios.markdown_render import render
        from ssios.notes_index import section
        from ssios.vault import _split_frontmatter, read_note

        def embed(target: str):
            name, _sep, fragment = target.partition("#")
            hit = self.view.resolver.resolve(current, name) if name else current
            if not hit:
                return None
            if hit.casefold().endswith(".pdf"):
                src = html.escape("/ui/vault/asset?path=" + _q(hit), quote=True)
                return f'<div class="embed embed-pdf"><iframe src="{src}" title="{html.escape(hit, quote=True)}"></iframe></div>'
            if not hit.casefold().endswith(".md"):
                return None
            title = html.escape(self.view.title(hit) + (" › " + fragment if fragment else ""))
            link = html.escape(note_href(hit, anchor=_anchor(fragment)), quote=True)
            if depth >= 3 or hit in stack:
                return f'<div class="embed embed-stop"><a href="{link}">{title}</a> <span class="muted">is already shown above.</span></div>'
            try:
                text = read_note(self.plane.settings, self.account_id, hit)
            except SsiosError:
                return None
            part = section(text, fragment) if fragment else _split_frontmatter(text)[1]
            if not part.strip():
                return f'<div class="embed"><div class="embed-title"><a href="{link}">{title}</a></div><p class="muted">That section is empty or not there.</p></div>'
            inner = render(part, self.link_for(hit), self.embed_for(hit, depth + 1, stack + (hit,)))
            return f'<div class="embed"><div class="embed-title"><a href="{link}">{title}</a></div>{inner}</div>'

        return embed

    def render(self, rel: str, body: str) -> str:
        from ssios.markdown_render import render

        return render(body, self.link_for(rel), self.embed_for(rel, 0, (rel,)))


def _group(now: datetime, stamp: float) -> str:
    days = (now.date() - datetime.fromtimestamp(stamp, now.tzinfo).date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days <= 7:
        return "Previous 7 days"
    if days <= 30:
        return "Previous 30 days"
    return "Older"


def _row(item: dict, chosen: str, href: str, label: str) -> str:
    mark = " active" if item["path"] == chosen else ""
    kind = "" if item["path"].casefold().endswith(".md") else f' <span class="kind">{html.escape(item["path"].rsplit(".", 1)[-1].upper())}</span>'
    drag = ' draggable="true"' if item["path"].casefold().endswith(".md") else ""
    return (
        f'<a class="vault-row{mark}"{drag} data-path="{html.escape(item["path"], quote=True)}" href="{html.escape(href, quote=True)}" title="{html.escape(item["path"], quote=True)}" '
        f'data-name="{html.escape((label + " " + item["path"]).casefold(), quote=True)}">{html.escape(label)}{kind}</a>'
    )


def _label(item: dict) -> str:
    name = item["path"].rsplit("/", 1)[-1]
    return name[:-3] if name.casefold().endswith(".md") else name


def _tree(items: list[dict], chosen: str, keep: dict) -> str:
    tree: dict = {}
    for item in items:
        node = tree
        parts = item["path"].split("/")
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = item
    opened = set()
    if chosen:
        parts = chosen.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            opened.add("/".join(parts[:index]))

    def draw(node: dict, prefix: str) -> str:
        folders = sorted((key for key in node if key.endswith("/")), key=str.casefold)
        files = sorted((key for key in node if not key.endswith("/")), key=str.casefold)
        out = []
        for key in folders:
            path = prefix + key[:-1]
            count = _count(node[key])
            attr = " open" if path in opened else ""
            out.append(
                f'<details class="tree-folder" data-folder="{html.escape(path, quote=True)}"{attr}><summary>{html.escape(key[:-1])}'
                f' <span class="count">{count}</span></summary>{draw(node[key], path + "/")}</details>'
            )
        for key in files:
            item = node[key]
            out.append(_row(item, chosen, note_href(item["path"], **keep), _label(item)))
        return "".join(out)

    return draw(tree, "") or '<p class="muted">No notes yet.</p>'


def _count(node: dict) -> int:
    return sum(_count(value) if key.endswith("/") else 1 for key, value in node.items())


def _local_graph(view, rel: str, outgoing: list[dict], backlinks: list[dict]) -> str:
    from ssios.notes_index import links_in

    neighbours: list[str] = []
    for row in outgoing:
        if row["path"] and row["path"].casefold().endswith(".md") and row["path"] != rel and row["path"] not in neighbours:
            neighbours.append(row["path"])
    for row in backlinks:
        if row["path"] != rel and row["path"] not in neighbours:
            neighbours.append(row["path"])
    neighbours = neighbours[:24]
    if not neighbours:
        return '<p class="muted">No links to or from this note.</p>'
    size = 300
    centre = size / 2
    radius = 104
    points = {rel: (centre, centre)}
    for index, other in enumerate(neighbours):
        angle = 2 * math.pi * index / len(neighbours) - math.pi / 2
        points[other] = (centre + radius * math.cos(angle), centre + radius * math.sin(angle))
    texts = view.texts()
    lines = []
    for other in neighbours:
        x, y = points[other]
        lines.append(f'<line x1="{centre:.1f}" y1="{centre:.1f}" x2="{x:.1f}" y2="{y:.1f}"/>')
    for index, source in enumerate(neighbours):
        for link in links_in(texts.get(source, "")):
            hit = view.resolver.resolve(source, link["target"]) if link["target"] else ""
            if hit in points and hit not in (source, rel) and neighbours.index(hit) > index:
                (x1, y1), (x2, y2) = points[source], points[hit]
                lines.append(f'<line class="side" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
    nodes = []
    for path, (x, y) in points.items():
        label = html.escape(view.title(path)[:16])
        css = "node current" if path == rel else "node"
        anchor_x = "middle" if path == rel else ("start" if x >= centre else "end")
        dx = 0 if path == rel else (7 if x >= centre else -7)
        nodes.append(
            f'<a href="{html.escape(note_href(path), quote=True)}"><g class="{css}"><circle cx="{x:.1f}" cy="{y:.1f}" r="{6 if path == rel else 4.5}"/>'
            f'<text x="{x + dx:.1f}" y="{y + (16 if path == rel else 3.5):.1f}" text-anchor="{anchor_x}">{label}</text>'
            f"<title>{html.escape(path)}</title></g></a>"
        )
    return f'<svg class="local-graph" viewBox="-60 0 {size + 120} {size}" role="img" aria-label="Local graph">{"".join(lines)}{"".join(nodes)}</svg>'


def _info_pane(view, rel: str, text: str, csrf: str = "") -> str:
    from ssios.notes_index import outline

    heads = outline(text)
    outline_html = "".join(
        f'<a class="outline-row" style="padding-left:{(row["level"] - 1) * 12 + 4}px" href="#{html.escape(row["anchor"], quote=True)}">{html.escape(row["text"])}</a>'
        for row in heads
    ) or '<p class="muted">No headings.</p>'
    back = view.backlinks(rel)
    cards = [
        f'<div class="link-card"><a href="{html.escape(note_href(row["path"]), quote=True)}">{html.escape(row["title"])}</a>'
        + "".join(f'<p class="context">{html.escape(line)}</p>' for line in row["contexts"])
        + "</div>"
        for row in back
    ]
    back_html = "".join(cards[:10]) + (f'<details class="more"><summary>{len(cards) - 10} more</summary>{"".join(cards[10:])}</details>' if len(cards) > 10 else "")
    back_html = back_html or '<p class="muted">No backlinks.</p>'
    mentions = view.unlinked(rel, {row["path"] for row in back}) if rel.casefold().endswith(".md") else []
    hidden = f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}"><input type="hidden" name="path" value="{html.escape(rel, quote=True)}">'
    mention_html = "".join(
        f'<div class="link-card"><a href="{html.escape(note_href(row["path"]), quote=True)}">{html.escape(row["title"])}</a>'
        + "".join(f'<p class="context">{html.escape(line)}</p>' for line in row["contexts"])
        + f'<form method="post" action="/ui/vault/note">{hidden}<input type="hidden" name="source" value="{html.escape(row["path"], quote=True)}">'
        '<button name="op" value="link-mention" type="submit" title="Turn this mention into a link">Link</button></form>'
        + "</div>"
        for row in mentions
    ) or '<p class="muted">None.</p>'
    outgoing = view.outgoing(rel, text)
    out_html = "".join(
        (
            f'<a class="outline-row" href="{html.escape(note_href(row["path"]), quote=True)}">{html.escape(row["title"])}</a>'
            if row["path"]
            else f'<span class="outline-row unresolved" title="Not created yet">{html.escape(row["target"])}</span>'
        )
        for row in outgoing
    ) or '<p class="muted">No links.</p>'
    partial = (
        f'<p class="muted small">{view.missed} notes were not read in time, so backlinks may be missing. They are read on the next visit.</p>'
        if view.missed
        else ""
    )
    return (
        '<aside class="vault-info">'
        f'<details open><summary>Outline <span class="count">{len(heads)}</span></summary>{outline_html}</details>'
        f'<details open><summary>Backlinks <span class="count">{len(back)}</span></summary>{partial}{back_html}</details>'
        f'<details><summary>Unlinked mentions <span class="count">{len(mentions)}</span></summary>{mention_html}</details>'
        f'<details open><summary>Outgoing links <span class="count">{len(outgoing)}</span></summary>{out_html}</details>'
        f'<details open><summary>Local graph</summary>{_local_graph(view, rel, outgoing, back)}</details>'
        "</aside>"
    )


def _properties(props: dict, renderer: Renderer, rel: str) -> str:
    if not props:
        return ""
    rows = []
    link_for = renderer.link_for(rel)
    for key, value in props.items():
        values = value if isinstance(value, list) else [value]
        cells = []
        for one in values:
            text = str(one)
            if key.casefold() in {"tags", "tag"}:
                name = text.lstrip("#")
                cells.append(f'<a class="chip" href="/ui/vault?view=tags&amp;tag={html.escape(_q(name), quote=True)}">#{html.escape(name)}</a>')
            elif text.startswith("[[") and text.endswith("]]"):
                target = text[2:-2].split("|")[0]
                href = link_for(target)
                cells.append(f'<a class="chip" href="{html.escape(href, quote=True)}">{html.escape(target)}</a>' if href else html.escape(text))
            elif text.startswith(("https://", "http://")):
                cells.append(f'<a href="{html.escape(text, quote=True)}" rel="noopener noreferrer">{html.escape(text)}</a>')
            else:
                cells.append(html.escape(text) or '<span class="muted">empty</span>')
        rows.append(f"<dt>{html.escape(key)}</dt><dd>{' '.join(cells)}</dd>")
    return f'<details class="vault-props" open><summary>Properties</summary><dl>{"".join(rows)}</dl></details>'


def _note_tools(session, rel: str, depth: str, mode: str, starred: bool, keep: dict, update: bool = True) -> str:
    from ssios.reading import depth_label

    csrf = html.escape(str(session.get("csrf") or ""), quote=True)
    path = html.escape(rel, quote=True)
    hidden = f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="path" value="{path}">'
    toggle = (
        f'<a class="button" href="{html.escape(note_href(rel, **keep), quote=True)}" title="Reading view (Ctrl+E)">Read</a>'
        if mode == "edit"
        else f'<a class="button primary" href="{html.escape(note_href(rel, mode="edit", **keep), quote=True)}" title="Edit (Ctrl+E)">Edit</a>'
    )
    star = "★" if starred else "☆"
    return (
        '<div class="vault-tools">'
        f'<span class="badge depth-{html.escape(depth, quote=True)}" title="{html.escape(_TOOL_HINT, quote=True)}">{depth_label(depth)}</span>'
        f'<form method="post" action="/ui/vault/note">{hidden}'
        + ('<button name="op" value="read" type="submit">Mark as read</button>' if depth != "read" else "")
        + f'<button name="op" value="bookmark" type="submit" title="Bookmark" aria-label="Bookmark">{star}</button></form>'
        f"{toggle}"
        '<details class="menu"><summary aria-label="More">⋯</summary><div class="menu-pop">'
        f'<form method="post" action="/ui/vault/note">{hidden}<label>Remind me to read <select name="when"><option value="tonight">tonight</option>'
        '<option value="tomorrow" selected>tomorrow morning</option><option value="weekend">this weekend</option><option value="week">in a week</option></select></label>'
        '<button name="op" value="remind" type="submit">Set reminder</button></form>'
        f'<form method="post" action="/ui/vault/note">{hidden}<label>Rename or move to <input name="new_path" value="{path}" maxlength="300"></label>'
        f'<label class="inline"><input type="checkbox" name="update_links" value="1"{" checked" if update else ""}> Update links in other notes</label>'
        '<button name="op" value="rename" type="submit">Rename</button></form>'
        f'<form method="post" action="/ui/vault/note">{hidden}<label class="inline"><input type="checkbox" name="confirm" value="1" required> Move this note to the vault trash</label>'
        '<button name="op" value="trash" type="submit">Move to trash</button></form>'
        "</div></details></div>"
    )


def _editor(session, rel: str, text: str, base: str, notice: str = "", disk: str | None = None) -> str:
    csrf = html.escape(str(session.get("csrf") or ""), quote=True)
    conflict = ""
    if disk is not None:
        conflict = (
            '<div class="notice conflict"><p><strong>This note changed on disk after you opened it.</strong> Your text is below and was not saved.</p>'
            f'<form method="post" action="/ui/vault/note"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="path" value="{html.escape(rel, quote=True)}">'
            f'<input type="hidden" name="base" value="{html.escape(_hash(disk), quote=True)}"><textarea name="text" hidden>{html.escape(text)}</textarea>'
            '<button name="op" value="save" type="submit">Save mine over it</button> '
            f'<a class="button" href="{html.escape(note_href(rel, mode="edit"), quote=True)}">Discard mine and reload</a></form>'
            f'<details><summary>The version on disk</summary><pre class="disk">{html.escape(disk)}</pre></details></div>'
        )
    return (
        f"{notice}{conflict}"
        f'<form id="note-editor" class="note-editor" method="post" action="/ui/vault/note" data-path="{html.escape(rel, quote=True)}">'
        f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="path" value="{html.escape(rel, quote=True)}">'
        f'<input type="hidden" name="base" value="{html.escape(base, quote=True)}">'
        '<div class="editor-panes"><textarea name="text" spellcheck="true" aria-label="Note text">'
        + html.escape(text)
        + '</textarea><article class="vault-note editor-preview" aria-live="polite"></article></div>'
        '<div class="editor-bar"><span class="editor-state muted small">Saved</span>'
        '<span class="muted small">Ctrl+S saves. Paste an image to attach it. Ctrl+E returns to reading.</span>'
        '<button name="op" value="save" type="submit" class="primary">Save and read</button></div></form>'
    )


def _hash(text: str) -> str:
    from ssios.notes_edit import content_hash

    return content_hash(text)


def _side(plane, session, view, chosen: str, mode_view: str, tag: str, query: str, vault_name: str) -> str:
    from ssios.calendar_intent import host_clock
    from ssios.notes_edit import bookmarks, new_note_folder, templates
    from ssios.ui import _account

    keep = {"view": mode_view, "tag": tag, "query": query}
    items = view.notes + view.attachments
    tabs = "".join(
        f'<a class="{"active" if mode_view == name else ""}" href="/ui/vault?view={name}" title="{label}">{label}</a>'
        for name, label in (("files", "Files"), ("recent", "Recent"), ("search", "Search"), ("tags", "Tags"), ("bookmarks", "Bookmarks"))
    )
    body = ""
    if mode_view == "recent":
        now, _zone = host_clock(plane, _account(session))
        groups: dict[str, list[str]] = {}
        for item in sorted(items, key=lambda row: row.get("mtime") or 0, reverse=True):
            groups.setdefault(_group(now, float(item.get("mtime") or 0)), []).append(_row(item, chosen, note_href(item["path"], **keep), _label(item)))
        body = "".join(
            f'<details class="vault-group" open><summary>{name} <span class="count">{len(groups[name])}</span></summary>{"".join(groups[name])}</details>'
            for name in _GROUPS
            if name in groups
        ) or '<p class="muted">No notes yet.</p>'
    elif mode_view == "search":
        found = view.search(query) if query.strip() else {"results": [], "total": 0, "missed": 0}
        rows = []
        for row in found["results"]:
            snippets = "".join(f'<p class="snippet">{piece}</p>' for piece in row["snippets"])
            rows.append(
                f'<a class="search-hit{" active" if row["path"] == chosen else ""}" href="{html.escape(note_href(row["path"], **keep), quote=True)}">'
                f'<strong>{html.escape(_label(row))}</strong> <span class="muted small">{html.escape(row["path"].rsplit("/", 1)[0] if "/" in row["path"] else "")}</span>{snippets}</a>'
            )
        summary = ""
        if query.strip():
            summary = f'<p class="muted small">{found.get("total", 0)} notes match.' + (f' {found["missed"]} notes were not read in time.' if found.get("missed") else "") + "</p>"
        body = (
            '<form class="vault-search" method="get" action="/ui/vault"><input type="hidden" name="view" value="search">'
            f'<input id="vault-search" type="search" name="q" value="{html.escape(query, quote=True)}" placeholder="Search notes" aria-label="Search notes">'
            '<p class="muted small">Words, "a phrase", -exclude, tag:name, path:folder, file:name</p></form>'
            + summary
            + "".join(rows)
        )
    elif mode_view == "tags":
        counts: dict[str, int] = {}
        for note in view.notes:
            for name in note.get("tags") or []:
                counts[name] = counts.get(name, 0) + 1
                parts = name.split("/")
                for index in range(1, len(parts)):
                    parent = "/".join(parts[:index])
                    counts.setdefault(parent, 0)
        tag_rows = "".join(
            f'<a class="tag-row{" active" if name == tag else ""}" style="padding-left:{8 + name.count("/") * 14}px" href="/ui/vault?view=tags&amp;tag={html.escape(_q(name), quote=True)}">'
            f'#{html.escape(name.rsplit("/", 1)[-1])} <span class="count">{count or ""}</span></a>'
            for name, count in sorted(counts.items(), key=lambda pair: pair[0].casefold())
        ) or '<p class="muted">No tags.</p>'
        tagged = ""
        if tag:
            hits = [note for note in view.notes if any(one == tag or one.startswith(tag + "/") for one in note.get("tags") or [])]
            tagged = f'<p class="muted small">#{html.escape(tag)}: {len(hits)} notes <a href="/ui/vault?view=tags">Clear</a></p>' + "".join(
                _row(note, chosen, note_href(note["path"], **keep), _label(note)) for note in hits
            )
        body = tagged + f'<div class="tag-list">{tag_rows}</div>'
    elif mode_view == "bookmarks":
        marks = bookmarks(plane.settings, session["account"]["id"])
        known = {item["path"]: item for item in items}
        body = "".join(
            _row(known[mark["path"]], chosen, note_href(mark["path"], **keep), mark.get("title") or _label(known[mark["path"]]))
            + ('<span class="muted small from">from Obsidian</span>' if mark["source"] == "obsidian" else "")
            for mark in marks
            if mark["path"] in known
        ) or '<p class="muted">No bookmarks. Use ☆ on a note.</p>'
    else:
        body = '<input id="vault-filter" type="search" placeholder="Filter files" aria-label="Filter files">' + _tree(items, chosen, keep)
        body += '<p class="muted small tree-hint">Drag a note onto a folder to move it, or onto this list for the vault root.</p>'
    csrf = html.escape(str(session.get("csrf") or ""), quote=True)
    folder = new_note_folder(plane.settings, session["account"]["id"], chosen)
    options = "".join(f'<option value="{html.escape(rel, quote=True)}">{html.escape(rel)}</option>' for rel in templates(plane.settings, session["account"]["id"]))
    template_field = f'<label>Template <select name="template"><option value="">None</option>{options}</select></label>' if options else ""
    new = (
        '<details class="vault-new"><summary title="New note">+ New</summary>'
        f'<form method="post" action="/ui/vault/note"><input type="hidden" name="csrf" value="{csrf}">'
        f'<label>Name <input name="name" placeholder="Untitled" maxlength="120"></label>'
        f'<label>Folder <input name="folder" value="{html.escape(folder, quote=True)}" maxlength="200"></label>{template_field}'
        '<button name="op" value="create" type="submit">Create note</button></form></details>'
        f'<form method="post" action="/ui/vault/note"><input type="hidden" name="csrf" value="{csrf}"><button name="op" value="daily" type="submit" title="Open today\'s daily note">Today</button></form>'
        '<button type="button" id="open-switcher" title="Quick switcher (Ctrl+O)">⌘O</button>'
        '<a class="button" href="/ui/vault?view=graph" title="Graph of the vault">Graph</a>'
    )
    clip = (
        '<details class="vault-clip"><summary>Clip a page</summary>'
        f'<form method="post" action="/ui/vault" enctype="multipart/form-data"><input type="hidden" name="csrf" value="{csrf}">'
        '<label>Source URL <input name="url" placeholder="https://example.com/story"></label>'
        '<label>Pasted article <textarea name="content" rows="4"></textarea></label>'
        '<label>Or a text file <input type="file" name="file"></label>'
        '<button type="submit">Clip pasted article</button></form></details>'
    )
    return (
        '<aside class="vault-side">'
        f'<div class="vault-head"><strong title="Vault">{html.escape(vault_name)}</strong><span class="vault-actions">{new}</span></div>'
        f'<nav class="vault-tabs">{tabs}</nav>'
        f'<div class="vault-list"{" data-root" if mode_view == "files" else ""}>{body}</div>{clip}</aside>'
    )


def vault_page(plane, session: dict, query: dict, notice: str = "", *, editing: dict | None = None) -> str:
    from ssios.notes_edit import bookmarks
    from ssios.notes_index import VaultView
    from ssios.reading import ledger
    from ssios.ui import VAULT_PATH, _clip_notice, _page
    from ssios.vault import account_vault, own_vault, properties, read_note

    account_id = session["account"]["id"]
    mode_view = str(query.get("view") or "files")
    mode_view = mode_view if mode_view in _VIEWS else "files"
    tag = str(query.get("tag") or "")
    if tag and mode_view == "files":
        mode_view = "tags"
    search = str(query.get("q") or "")[:300]
    rel = str(query.get("path") or "")
    mode = "edit" if str(query.get("mode") or "") == "edit" else "read"
    try:
        view = VaultView(plane.settings, account_id)
        vault_name = account_vault(plane.settings, account_id).name if own_vault(plane.settings, account_id) is not None else "Notes"
    except SsiosError as exc:
        if exc.code != "path_rejected":
            raise
        from ssios.locale_messages import say
        from ssios.ui import ui_language

        detail = exc.detail if "vault folder" in exc.detail else say(ui_language(plane, session), "clip_denied")
        body = _clip_notice("clip_denied", detail) + '<p class="muted">Set the vault again with <code>ssios vault-set</code>.</p>'
        return _page(session, "Notes", body, bleed=True)
    known = {item["path"] for item in view.notes + view.attachments}
    if not rel and mode_view != "graph":
        pool = view.notes
        if tag:
            pool = [note for note in view.notes if any(one == tag or one.startswith(tag + "/") for one in note.get("tags") or [])]
        rel = next((note["path"] for note in sorted(pool, key=lambda row: row.get("mtime") or 0, reverse=True) if note["path"] not in {"Tasks.md", "SSIOS/Tasks.md"}), "")
    keep = {"view": mode_view if mode_view != "graph" else "", "tag": tag, "query": search}
    side = _side(plane, session, view, rel, mode_view if mode_view != "graph" else "files", tag, search, vault_name)
    info = '<aside class="vault-info"></aside>'
    main = '<p class="muted">Choose a note on the left, or press Ctrl+O.</p>'
    renderer = Renderer(plane, account_id, view)
    if mode_view == "graph":
        data = view.graph()
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        main = (
            '<header class="vault-bar"><p class="vault-crumbs">Graph of the vault</p>'
            '<span class="graph-controls"><input id="graph-filter" type="search" placeholder="Filter notes" aria-label="Filter notes">'
            '<label class="inline"><input id="graph-orphans" type="checkbox" checked> Orphans</label>'
            '<label class="inline"><input id="graph-labels" type="checkbox"> All labels</label></span>'
            f'<p class="muted small">{len(data["nodes"])} notes, {len(data["edges"])} links. Colour is the top folder. Drag to move, scroll to zoom, click a note to open it.</p>'
            '<div id="graph-legend" class="graph-legend"></div></header>'
            f'<canvas id="vault-graph" class="vault-graph" width="1200" height="800"></canvas><script type="application/json" id="graph-data">{payload}</script>'
        )
    elif rel and rel not in known and rel.casefold().endswith(".md"):
        if str(query.get("missing") or "") == "1":
            csrf = html.escape(str(session.get("csrf") or ""), quote=True)
            main = (
                f'<header class="vault-bar"><p class="vault-crumbs">{html.escape(rel)}</p></header>'
                f'<div class="empty-note"><p>This note does not exist yet.</p><form method="post" action="/ui/vault/note"><input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="path" value="{html.escape(rel, quote=True)}"><button class="primary" name="op" value="create-missing" type="submit">Create it</button></form></div>'
            )
        else:
            main = f'<p class="notice">There is no note at {html.escape(rel)}.</p>'
    elif rel and rel.casefold().endswith(".md"):
        text = read_note(plane.settings, account_id, rel)
        props, body = properties(text)
        depth = ledger(plane.accounts.home(account_id)).get("note:" + rel, {}).get("depth") or "skimmed"
        starred = any(mark["path"] == rel for mark in bookmarks(plane.settings, account_id))
        crumbs = " / ".join(html.escape(part) for part in [vault_name] + rel[:-3].split("/"))
        from ssios.notes_edit import link_settings

        links = link_settings(plane.settings, account_id)
        panes = '<span class="pane-toggles"><button type="button" data-pane="left" title="Show or hide the file list">◧</button><button type="button" data-pane="right" title="Show or hide links and outline">◨</button></span>'
        bar = f'<header class="vault-bar">{panes}<p class="vault-crumbs" title="{html.escape(rel, quote=True)}">{crumbs}</p>{_note_tools(session, rel, depth, mode, starred, keep, links["update"])}</header>'
        if editing is not None or mode == "edit":
            state = editing or {}
            main = bar + _editor(session, rel, state.get("text", text), state.get("base", _hash(text)), "", state.get("disk")).replace(
                'id="note-editor"', f'id="note-editor" data-link-format="{links["format"]}" data-markdown="{1 if links["markdown"] else 0}"', 1
            )
        else:
            first = next((line for line in body.splitlines() if line.strip()), "")
            title = "" if first.lstrip().startswith("# ") else f'<h1 class="vault-title">{html.escape(rel.rsplit("/", 1)[-1][:-3])}</h1>'
            main = f'{bar}<div class="vault-doc">{title}{_properties(props, renderer, rel)}<article class="vault-note">{renderer.render(rel, body)}</article></div>'
        info = _info_pane(view, rel, text, str(session.get("csrf") or ""))
    elif rel:
        src = html.escape("/ui/vault/asset?path=" + _q(rel), quote=True)
        shown = f'<iframe class="vault-pdf" src="{src}" title="{html.escape(rel, quote=True)}"></iframe>' if rel.casefold().endswith(".pdf") else f'<img class="vault-image" src="{src}" alt="">'
        main = f'<header class="vault-bar"><p class="vault-crumbs">{html.escape(rel)}</p></header>{shown}'
        info = _info_pane(view, rel, "")
    # The editor's preview can show math the saved note does not have yet.
    editing_now = 'id="note-editor"' in main
    math = '<link rel="stylesheet" href="/ui/vendor/katex/katex.min.css"><script src="/ui/vendor/katex/katex.min.js"></script>' if ('class="math' in main or editing_now) else ""
    if 'class="mermaid"' in main or editing_now:
        math += '<script src="/ui/vendor/mermaid/mermaid.min.js"></script>'
    css = " graph-mode" if mode_view == "graph" else ""
    body = (
        f'{math}<div class="vault{css}" data-csrf="{html.escape(str(session.get("csrf") or ""), quote=True)}" data-path="{html.escape(rel, quote=True)}" data-mode="{mode}">'
        f'{side}<section class="vault-main">{notice}{main}</section>{info}</div>'
        '<div id="switcher" class="switcher" hidden><div class="switcher-box"><input id="switcher-input" type="search" placeholder="Find a note or file" aria-label="Find a note or file">'
        '<div id="switcher-list" class="switcher-list" role="listbox"></div><p class="muted small">↑ ↓ to choose, Enter to open, Esc to close</p></div></div>'
        f'<script src="{VAULT_PATH}"></script>'
    )
    return _page(session, "Notes", body, bleed=True)


def index_json(plane, session: dict):
    from ssios.notes_index import _excluded
    from ssios.ui import Page
    from ssios.vault import list_attachments, list_notes

    account_id = session["account"]["id"]
    rules = _excluded(plane.settings, account_id)
    rows = [{"p": note["path"], "t": note.get("title") or "", "a": note.get("aliases") or []} for note in list_notes(plane.settings, account_id) if not any(rule(note["path"]) for rule in rules)]
    rows += [{"p": item["path"], "t": "", "a": []} for item in list_attachments(plane.settings, account_id)]
    return Page(200, json.dumps(rows, ensure_ascii=False), content_type="application/json; charset=utf-8")


def peek(plane, session: dict, query: dict):
    """A linked note's start, for the hover preview. Same account, same renderer, embeds one level deep."""
    from ssios.notes_index import VaultView, section
    from ssios.ui import Page
    from ssios.vault import properties, read_note

    account_id = session["account"]["id"]
    rel = str(query.get("path") or "")
    fragment = str(query.get("h") or "")
    text = read_note(plane.settings, account_id, rel)
    _props, body = properties(text)
    if fragment:
        body = section(text, fragment) or body
    lines = body.split("\n")
    kept, size = [], 0
    for line in lines:
        kept.append(line)
        size += len(line)
        if size > 2500:
            kept.append("\n…")
            break
    view = VaultView(plane.settings, account_id, budget=0.2)
    renderer = Renderer(plane, account_id, view)
    from ssios.markdown_render import render

    markup = render("\n".join(kept), renderer.link_for(rel), renderer.embed_for(rel, 2, (rel,)))
    head = f'<div class="peek-title">{html.escape(rel[:-3] if rel.casefold().endswith(".md") else rel)}</div>'
    return Page(200, head + f'<article class="vault-note">{markup}</article>', content_type="text/html; charset=utf-8")


def preview(plane, session: dict, body: dict):
    from ssios.notes_index import VaultView
    from ssios.ui import Page
    from ssios.vault import properties

    rel = str(body.get("path") or "")
    _props, text = properties(str(body.get("text") or "")[:2_000_000])
    view = VaultView(plane.settings, session["account"]["id"], budget=0.2)
    rendered = Renderer(plane, session["account"]["id"], view).render(rel, text)
    return Page(200, rendered, content_type="text/html; charset=utf-8")


def _audit(plane, session, peer_ip: str, action: str, result: str = "ok") -> None:
    plane.security_audit.write(actor=session["account"]["id"], ip=peer_ip or "127.0.0.1", action=action, target="note", result=result)


def note_post(plane, session: dict, body: dict, peer_ip: str):
    """Every change made from the Notes page. The path comes from the form and is checked against the vault on each step."""
    from ssios.calendar_intent import host_clock
    from ssios import notes_edit
    from ssios.reading import mark_note, remind_to_read
    from ssios.scope import AccountScope
    from ssios.ui import Page, _account
    from ssios.vault import read_note

    account_id = session["account"]["id"]
    settings = plane.settings
    op = str(body.get("op") or "")
    rel = str(body.get("path") or "")
    ajax = str(body.get("ajax") or "") == "1"
    now, _zone = host_clock(plane, _account(session))
    now = now.replace(tzinfo=None) if now.tzinfo is None else now

    def redirect(target: str, mode: str = "") -> Page:
        return Page(303, "", location=note_href(target, mode=mode))

    def json_reply(payload: dict, status: int = 200) -> Page:
        return Page(status, json.dumps(payload, ensure_ascii=False), content_type="application/json; charset=utf-8")

    if op == "save":
        try:
            base = notes_edit.write_note(settings, account_id, rel, str(body.get("text") or ""), str(body.get("base") or ""))
        except SsiosError as exc:
            if exc.code != "note_conflict":
                if ajax:
                    return json_reply({"ok": False, "error": exc.detail}, exc.status)
                raise
            if ajax:
                return json_reply({"ok": False, "conflict": True, "error": exc.detail}, 409)
            return Page(409, vault_page(plane, session, {"path": rel, "mode": "edit"}, editing={"text": str(body.get("text") or ""), "base": str(body.get("base") or ""), "disk": getattr(exc, "current", "")}))
        _audit(plane, session, peer_ip, "vault.write")
        if ajax:
            return json_reply({"ok": True, "base": base})
        return redirect(rel)
    if op == "attach":
        data = notes_edit.decode_image(str(body.get("data") or ""))
        saved = notes_edit.save_attachment(settings, account_id, rel, data, now)
        _audit(plane, session, peer_ip, "vault.attach")
        from ssios.notes_index import VaultView

        embed = notes_edit.link_text(saved, rel, VaultView(settings, account_id, budget=0).resolver, notes_edit.link_settings(settings, account_id), embed=True)
        return json_reply({"ok": True, "path": saved, "embed": embed})
    if op in {"create", "create-missing"}:
        if op == "create-missing":
            target = notes_edit.create_note(settings, account_id, rel)
        else:
            target = notes_edit.free_path(settings, account_id, str(body.get("folder") or ""), str(body.get("name") or "") or "Untitled")
            text = ""
            template = str(body.get("template") or "")
            if template:
                text = notes_edit.template_text(settings, account_id, template, target.rsplit("/", 1)[-1][:-3], now)
            notes_edit.create_note(settings, account_id, target, text)
        _audit(plane, session, peer_ip, "vault.create")
        return redirect(target, "edit")
    if op == "daily":
        target, created = notes_edit.daily_note(settings, account_id, now)
        if created:
            _audit(plane, session, peer_ip, "vault.create")
        return redirect(target, "edit" if created else "")
    read_note(settings, account_id, rel) if rel.casefold().endswith(".md") else None
    if op == "rename":
        new = str(body.get("new_path") or "").strip().strip("/")
        if new and not new.casefold().endswith(".md"):
            new += ".md"
        parts = [notes_edit.clean_name(part) for part in new[:-3].split("/")] if new else []
        if not parts or any(not part for part in parts):
            raise SsiosError("note_rejected", "Give the note a name.", 400)
        update = str(body.get("update_links") or "") == "1"
        result = notes_edit.rename_note(settings, account_id, rel, "/".join(parts) + ".md", update_links=update)
        _audit(plane, session, peer_ip, "vault.rename")
        if ajax:
            return json_reply({"ok": True, "path": result["path"], "updated": len(result["updated"])})
        notice = f'<p class="notice">Renamed. Links updated in {len(result["updated"])} notes.' + (f' {len(result["skipped"])} notes changed while renaming and were left as they were.' if result["skipped"] else "") + "</p>"
        return Page(200, vault_page(plane, session, {"path": result["path"]}, notice))
    if op == "trash":
        if str(body.get("confirm") or "") != "1":
            raise SsiosError("note_rejected", "Tick the box to move the note to the trash.", 400)
        where = notes_edit.trash_note(settings, account_id, rel)
        _audit(plane, session, peer_ip, "vault.trash")
        return Page(200, vault_page(plane, session, {}, f'<p class="notice">Moved to {html.escape(where)}. Obsidian shows it under its trash.</p>'))
    if op == "link-mention":
        linked = notes_edit.link_mention(settings, account_id, str(body.get("source") or ""), rel)
        if linked:
            _audit(plane, session, peer_ip, "vault.write")
        return Page(200, vault_page(plane, session, {"path": rel}, f'<p class="notice">{"Linked." if linked else "That mention is no longer there."}</p>'))
    if op == "bookmark":
        on = notes_edit.toggle_bookmark(settings, account_id, rel)
        return Page(200, vault_page(plane, session, {"path": rel}, f'<p class="notice">{"Bookmarked." if on else "Bookmark removed."}</p>'))
    scope = AccountScope(plane, _account(session))
    if op == "remind":
        when = remind_to_read(plane, scope, "note:" + rel, rel.rsplit("/", 1)[-1][:-3], str(body.get("when") or "tomorrow"))
        return Page(200, vault_page(plane, session, {"path": rel}, f"<p class=\"notice\">Reminder set for {html.escape(when)}.</p>"))
    if op == "read":
        mark_note(plane, scope, rel, "read", "marked", peer_ip)
        return Page(200, vault_page(plane, session, {"path": rel}, '<p class="notice">Marked as read.</p>'))
    raise SsiosError("note_rejected", "That action is not known.", 400)

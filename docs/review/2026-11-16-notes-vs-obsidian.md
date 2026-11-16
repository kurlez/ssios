# Notes compared with Obsidian, 2026-11-16

Branch `claude/epic-bardeen-zdd6j2`. Obsidian here means the desktop app with its core plugins (file explorer, search, backlinks, outgoing links, outline, tags, properties, graph, daily notes, templates, bookmarks, quick switcher). Community plugins are out of scope. Each row is scored against what Obsidian does for that area. Scores are this reviewer's judgment, with the evidence named.

## Rubric

| # | Area | Weight | What full marks means |
| --- | --- | --- | --- |
| 1 | Rendering | 15 | Everything Obsidian draws in reading view: headings with anchors, lists, tasks, tables, callouts (also foldable), math, images with size, embedded notes and sections, footnotes, comments, code with highlighting, Mermaid |
| 2 | Navigation | 10 | File tree with nested folders, recent files, quick switcher, bookmarks, keyboard shortcuts |
| 3 | Search | 12 | Full text across the vault, snippets with the hit marked, phrases, `tag:` `path:` `file:` operators |
| 4 | Linking | 15 | Wiki and Markdown links, aliases, heading and block links, backlinks with context, unlinked mentions, outgoing links, unresolved links that create the note |
| 5 | Editing | 18 | Edit and save, live preview, conflict safety, new note, daily note, templates, rename or move with link updates, delete to trash, paste an image as an attachment |
| 6 | Structure | 10 | Tags pane with nesting and counts, properties shown with lists, outline of headings |
| 7 | Graph | 5 | Local graph of the open note and a graph of the whole vault |
| 8 | Vault compatibility | 8 | Reads the vault in place, never writes `.obsidian`, uses Obsidian's own settings where they matter, works with a vault on a cloud drive, sync tool |
| 9 | Interface | 7 | Clean three-pane layout, shortcuts, works on a phone, fast on a large or cloud-only vault |

## Round 0: before this work

| # | Area | Score | Evidence and gaps |
| --- | --- | --- | --- |
| 1 | Rendering | 10 / 15 | Headings, lists, tasks, tables, callouts, math, sized images. No heading anchors, no embedded notes, no footnotes, no foldable callouts, `%%comments%%` shown, no code highlighting, no Mermaid |
| 2 | Navigation | 4 / 10 | Recent list by day and a flat list of folders, a title filter. No tree, no quick switcher, no bookmarks, no shortcuts |
| 3 | Search | 1 / 12 | Only a filter on titles and paths in the list |
| 4 | Linking | 5 / 15 | `[[links]]`, aliases in labels, and images resolve across folders. No heading or block links, no backlinks, no outgoing links, an unresolved link is plain text, aliases in properties are ignored |
| 5 | Editing | 0 / 18 | Read-only |
| 6 | Structure | 5 / 10 | Tags with counts, a flat tag list, properties folded and lists in properties not parsed. No outline |
| 7 | Graph | 0 / 5 | None |
| 8 | Vault compatibility | 7 / 8 | Reads the vault in place, `.obsidian` untouched, `vault-set` and `vault-sync`, index for cloud vaults. Ignores Obsidian's daily-note, template, attachment, and bookmark settings |
| 9 | Interface | 4 / 7 | Two panes with a sticky note bar. No right-hand pane, no shortcuts, the layout stacks on a phone but the list is long |
| | **Total** | **36 / 100** | |

## Round 1: three panes, editor, search, backlinks, graph

| # | Area | Score | What changed, and what is still missing |
| --- | --- | --- | --- |
| 1 | Rendering | 13 / 15 | Heading anchors, embedded notes, sections, blocks, and PDFs, footnotes, foldable callouts, `%%comments%%`, code highlighting for common languages. Mermaid shown as source only |
| 2 | Navigation | 8.5 / 10 | Folder tree that remembers what is open, Recent, quick switcher with aliases (Ctrl+O), Bookmarks including Obsidian's, shortcuts. No drag to move, panes cannot be hidden |
| 3 | Search | 10 / 12 | Full text with marked snippets, phrases, `-word`, `tag:`, `path:`, `file:`, partial results reported on a cloud vault. No OR, hits not marked in the opened note |
| 4 | Linking | 12.5 / 15 | Resolution by path, name, and alias; heading and block links; backlinks with the linking line; unlinked mentions; outgoing links; an unresolved link opens a page that creates the note. No `[[` autocomplete |
| 5 | Editing | 14 / 18 | Source editor with live preview beside it, Ctrl+S, save refused when the file changed on disk with both versions shown, new note, daily note, templates, rename with link updates, trash, pasted images. No autocomplete, no inline live preview, Obsidian's link settings ignored |
| 6 | Structure | 9 / 10 | Nested tags pane with counts, properties with lists and tag chips, clickable outline. Properties are edited as text |
| 7 | Graph | 3.5 / 5 | Local graph in the right pane and a force-laid graph of the vault with pan, zoom, and click. No colours, filters, or orphan switch |
| 8 | Vault compatibility | 7.5 / 8 | Obsidian's new-note location, attachment folder, daily notes, templates, and bookmarks are read. A test checks `.obsidian` is never written. Link format settings ignored |
| 9 | Interface | 5.5 / 7 | Three panes, no overflow at phone width, per-request time limits and an in-process text cache. Panes cannot be hidden |
| | **Total** | **83.5 / 100** | |

## Round 2: closing the gaps round 1 left

| # | Area | Score | What changed, and what is still missing |
| --- | --- | --- | --- |
| 1 | Rendering | 14 / 15 | Mermaid drawn by the vendored Mermaid 10.9.1, with its source one click away. Inline footnotes `^[...]`, block IDs on list items. Highlighting covers about twenty languages in six families, not every language Obsidian's Prism does |
| 2 | Navigation | 9 / 10 | Drag a note onto a folder to move it, links updated. Hide either side pane. Still no tabs or split view |
| 3 | Search | 11 / 12 | `OR`, and the words searched are marked and scrolled to in the note opened from a result. No regular expressions or `line:` and `section:` operators |
| 4 | Linking | 14.5 / 15 | `[[` autocomplete in the editor, written in Obsidian's link format. A Link button turns an unlinked mention into a link. Hover over a link to see the note's start or the linked section. No heading autocomplete after `#` |
| 5 | Editing | 15.5 / 18 | Autocomplete, pasted images and renames follow `newLinkFormat` and `useMarkdownLinks`, and a rename can leave other notes alone, as `alwaysUpdateLinks` allows. Still no inline live preview, no form for properties, and folders cannot be renamed or removed from the page |
| 6 | Structure | 9 / 10 | Unchanged |
| 7 | Graph | 4.5 / 5 | Colour by top folder with a legend, orphan switch, filter, all labels. No custom groups or local-graph depth |
| 8 | Vault compatibility | 8 / 8 | Link format, Markdown links, and update-links settings are honoured, and Excluded files are left out of search, graph, mentions, and the switcher, as in Obsidian |
| 9 | Interface | 6 / 7 | Pane toggles kept per browser, autocomplete drops stale answers. Pane widths are fixed |
| | **Total** | **91 / 100** | |

### How this was checked

- `tests/test_notes_app.py` covers resolution, backlinks, mentions, outline, sections, search, saves and conflicts, line endings, daily notes and templates from Obsidian's settings, rename with link updates, trash, attachments, bookmarks, link formats, excluded files, hover preview, and every new route. Each test ends by checking that `.obsidian` was not changed.
- A browser (Chromium through Playwright) opened a synthetic 45-note vault and checked: the three panes, an embedded section, an unresolved link, the quick switcher by keyboard, Ctrl+E and Ctrl+S, the live preview with math, search marks in the opened note, pane toggles, `[[` autocomplete inserting `[[Hot wallet]]`, dragging a note into another folder, the Mermaid diagram drawn as SVG under the page's content policy, the hover preview, the graph with filter and legend, and no horizontal scroll at 390 px. No script errors were logged.
- Not checked: Safari, a real Obsidian vault, a vault on Google Drive, and two people editing the same note at once.

### What would move the score further

Inline live preview (editing in the rendered note), a properties form, folder rename and delete, tabs, `#heading` autocomplete, and regular-expression search.

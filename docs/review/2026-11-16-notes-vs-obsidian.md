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

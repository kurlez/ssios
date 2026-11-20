# Notes and an Obsidian vault

## The Notes page

Three panes, as in Obsidian.

- **Left**: Files (the folder tree), Recent, Search, Tags, and Bookmarks. `+ New` makes a note, with a template if Obsidian's Templates folder is set. `Today` opens the daily note. `Graph` shows the vault's links. Drag a note onto a folder to move it.
- **Middle**: the note. `Edit` (Ctrl+E) opens the source with a live preview beside it. Ctrl+S saves. Typing `[[` suggests notes. A pasted image is saved as an attachment. `⋯` has Remind me to read, Rename or move, and Move to trash.
- **Right**: Outline, Backlinks with the line that links, Unlinked mentions with a Link button, Outgoing links, and a local graph. Hover over a link to see the start of that note.

Shortcuts: Ctrl+O (or Cmd+O) quick switcher, Ctrl+Shift+F search, Ctrl+E read or edit, Ctrl+S save.

Search takes words, `"a phrase"`, `-exclude`, `tag:name`, `path:folder`, `file:name`, and `OR`. A note opened from the results has the words marked.

What SSIOS reads from `.obsidian`, without ever writing it: where new notes and attachments go, the daily-note folder, format, and template, the template folder, the link format (`newLinkFormat`, `useMarkdownLinks`, `alwaysUpdateLinks`), Excluded files, and bookmarks.

Saving is safe against another editor. A save carries the version it started from. If the file changed on disk in the meantime, for example because Obsidian saved it, nothing is written and both versions are shown. Trash moves a note into the vault's `.trash`, where Obsidian's own trash keeps it.

Renaming a note reads every note to update links, so on a vault that is still downloading from a cloud drive it can take a while.

Notes is an Obsidian vault on this computer. There are two ways to use it with a vault you already have. Both were tested on Linux with temporary folders, not with Obsidian running on a Mac.

## Use the Obsidian folder itself

```bash
./scripts/ssios vault-set --username owner --path "$HOME/Documents/MyVault"
```

Notes then reads that folder directly, so there is nothing to keep in sync. SSIOS writes only `Clippings/` (new clips and their images) and `SSIOS/Tasks.md` there. It does not write `.obsidian`, and it leaves a `Tasks.md` of your own alone. If the folder is missing, for example because its disk is not connected, Notes says so and writes nowhere else. `--path ""` goes back to the default folder under the state directory.

This is the simpler choice when Obsidian and SSIOS run on the same Mac.

The path may pass through a link, such as `~/Google Drive`. SSIOS follows it and stores the real folder, which for Google Drive is under `~/Library/CloudStorage/`. A vault in Google Drive or iCloud Drive should be set to stay on this Mac (Available offline, or Keep Downloaded). A file that only exists in the cloud is downloaded when it is read, which is slow, and it cannot be read at all without a connection.

SSIOS keeps this cheap. `vault-set` only counts file names. The Notes list opens a note the first time it sees it and again only when its size or time changes; the title, source, and tags are kept in `accounts/<id>/vault-index.json`, not the text. So the first visit to Notes reads the start of every note once, and later visits read none. Chat looks in notes whose name, title, or tags match the question first, and gives up on the rest after about a second and a half, so a note that matches only in its body may be missed while the vault is still cloud-only.

## Keep two folders the same

Use this when the Obsidian vault should stay a separate folder, for example one in iCloud Drive that other devices also use.

```bash
./scripts/ssios vault-sync --username owner --with "$HOME/Documents/MyVault" --dry-run
./scripts/ssios vault-sync --username owner --with "$HOME/Documents/MyVault"
```

`--dry-run` prints what would change and changes nothing. Run it first.

What is synced: notes (`.md`), canvases, and attachments (images, PDF, audio, and video up to 100 MB) in ordinary folders. `.obsidian`, `.trash`, other hidden folders and files, and symbolic links are left alone on both sides, so Obsidian's settings and plugins are never touched.

How a difference is settled. SSIOS keeps the hash of every file from the last sync in the state directory, so it knows which side changed.

| Since the last sync | Result |
| --- | --- |
| New on one side | Copied to the other |
| Changed on one side | Copied to the other |
| Changed on both sides | Both versions are kept on both sides. The other side's version is added beside the file as `name (conflict from Obsidian <time>).md` or `(conflict from SSIOS <time>)` |
| Same name on both sides at the first sync, different text | Treated as changed on both sides |
| Deleted on one side, unchanged on the other | The remaining copy is moved into that vault's `.trash` folder, where Obsidian keeps deleted notes |
| Deleted on one side, changed on the other | The changed file is copied back. An edit wins over a delete |

Nothing is overwritten unless only the other side changed, and nothing is deleted. A file is written to a temporary name and renamed, so a sync that is interrupted leaves whole files. Run it again to finish.

It is a command, not a background service. To run it on a schedule, use `launchd` or `cron`. Do not run two at once, and prefer a moment when Obsidian is not saving. A note saved in Obsidian between two syncs is picked up by the next one.

With iCloud Drive, a file that iCloud has removed from the Mac to save space is not readable until it is downloaded again. Turn on Keep Downloaded for the vault folder, or such files look deleted to the sync and the Notes copy is moved to `.trash`.

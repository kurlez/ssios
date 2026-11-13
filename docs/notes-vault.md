# Notes and an Obsidian vault

Notes is an Obsidian vault on this computer. There are two ways to use it with a vault you already have. Both were tested on Linux with temporary folders, not with Obsidian running on a Mac.

## Use the Obsidian folder itself

```bash
./scripts/ssios vault-set --username owner --path "$HOME/Documents/MyVault"
```

Notes then reads that folder directly, so there is nothing to keep in sync. SSIOS writes only `Clippings/` (new clips and their images) and `SSIOS/Tasks.md` there. It does not write `.obsidian`, and it leaves a `Tasks.md` of your own alone. If the folder is missing, for example because its disk is not connected, Notes says so and writes nowhere else. `--path ""` goes back to the default folder under the state directory.

This is the simpler choice when Obsidian and SSIOS run on the same Mac.

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

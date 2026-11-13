# Library folders, external disks, and Eagle

Tested on Linux with temporary folders and a synthetic Eagle library. Not yet run on a Mac, on an external disk, or against a library that Eagle itself wrote.

## A library in its own folder

By default a library is stored under the state directory. A library can instead keep its files in a folder you name, for example on an external disk.

```bash
./scripts/ssios library-create --username owner --name Photos --location /Volumes/Disk/Libraries
./scripts/ssios library-list --username owner
```

`--location` is a directory that already exists. SSIOS creates one folder inside it, named `Photos (lib-xxxxxxxx).ssioslibrary`, and keeps everything for that library there: originals, the catalog, thumbnails, and the search index. A marker file `ssios-library.json` in that folder names the library.

Refused as a location: a network address (`smb://`, `//host`), a path with a symbolic link in it, a path that does not exist, and anything inside or above the state directory.

An admin can also type the folder in Library settings on the Files page when creating a library. A member cannot name a folder on the page, because that makes this computer write outside the state directory. The operator sets it up for a member with the command above.

### When the disk is not connected

SSIOS looks for the marker file before every read or write. If it is missing, the library is shown as not connected, with the path where it is expected. Nothing is created at that path, so an unplugged disk never turns into an empty library on the internal disk. Chat search and background analysis skip the library. Connect the disk and reload.

### Permissions on the disk

Files are written as owner-only. APFS and Mac OS Extended keep that. exFAT and FAT have no permissions, so anyone who holds the disk can read the files. `library-list` shows no warning for that today; the result of `library-create` and `library-move` has `"permissions": "not enforced"` when the disk did not keep them. Use an encrypted APFS volume for anything private.

### Backup

`ssios backup` covers the state directory. A library in its own folder is not in the archive. Back up that disk separately.

### Sharing

Sharing a library that has its own folder leaves the files where they are. Only the member list is kept in the state directory.

## Move a library

```bash
./scripts/ssios library-move --username owner --library lib-0123abcd --location /Volumes/Disk/Libraries
```

The files are copied to a new folder, the copy is compared with the original by name and size, and only then does the library switch to the new folder. If the copy fails or does not match, the new folder is removed and nothing changes. The old files are not deleted. They are moved into a folder named `.moved-<time>` inside the old location, and the command prints that path as `old_copy`. Delete it yourself after you have checked the library. Until then the library takes twice the space.

Stop SSIOS, or do not add files to that library, while a move runs. A file added during the copy can be left behind in the old folder.

## Copy an Eagle library in

```bash
./scripts/ssios eagle-import --username owner --source "/Volumes/Disk/Design.library" --location /Volumes/Disk/Libraries
```

The Eagle folder is only read. Nothing in it is written, moved, or deleted, and SSIOS does not index it in place. `--location` is optional. Progress is printed while it runs, and a report at the end.

What is copied:

| Eagle | Files |
| --- | --- |
| Library name | A new library with that name |
| Folder tree | Folders, with the same nesting. An item in several folders stays in all of them |
| Tags and tag groups | Tags on the item, and the groups in the library catalog |
| Star | Star, 0 to 5 |
| Note | The note, up to 240 characters. The full text is kept with the Eagle fields |
| Source link | Kept on the item when it is an http or https address |
| Added and changed times | The item's times |
| Palette, width, height | Kept with the Eagle fields. Files does not show them yet |
| Smart folders | Kept as Eagle wrote them and counted in the report. They are not translated into Files smart folders |
| Thumbnails | Not copied. Files draws its own |
| Items in Eagle's trash | Not copied, and counted |

Each file passes the same rules as any import. Files stores images, movies, audio, PDF, EPUB, Office files, PSD, and fonts. A text file, an SVG, a web bookmark, an archive, or a program in the Eagle library is skipped, and the report counts them by extension. Location and device data are removed from PNG, JPEG, GIF, and WebP images, so those copies are not byte-for-byte the files Eagle holds.

Two Eagle items with the same bytes become one item that carries the tags and folders of both.

Running the command again on the same Eagle folder continues into the same library. Items already copied are skipped, so an interrupted import can be resumed and new Eagle items can be picked up later. A change made in Eagle to an item that was already copied is not picked up.

The audit log gets one line for the import, with the counts. It does not list file names.

### Limits

- The layout is the one importers describe for Eagle 3 and 4: `metadata.json` at the root and `images/<id>.info/` per item. Eagle does not publish it. Run the import on a copy of a small real library first and read the report.
- A large library takes time and the same space again. There is no dry run that only counts.
- The import runs from the command line, not from the page, because it can run for a long time.

## Export to Eagle later

Not built. The import keeps what an export needs: each item's Eagle id, its Eagle times, palette, size, full note, and source link, the folder map from Eagle ids to Files folders, the tag groups, and the smart folders exactly as Eagle wrote them. An export would write a new `.library` folder from that. It would never write into an existing Eagle library. An item added in Files has no Eagle id and would get a new one. Images would carry the stripped bytes.

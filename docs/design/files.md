# Files

The product name is Files. Source: the approved design page [ss 素材库设计（参考 Eagle）](https://cursor.com/canvas/store-2a23d281-d0e5-4488-9d1e-2d5885d5e221/0c934cb1-621a-474b-b6a0-838f6ac063ae). Eagle 4.0 is the reference for organization and import. It is not the product name, and it does not appear in the interface. This document is the English spec. The canvas stays the Chinese review copy. Numbers below are design estimates, not measurements from this machine.

Files is a personal drive inside SSIOS: one immutable original per content hash, virtual folders, tags, and (from M2) smart folders. The Mac mini is the only source of bytes. Other household devices use the existing LAN login. There is no cloud model fallback.

## Boundaries that do not change

- Local models only: `qwen3:14b` for text and `qwen2.5vl:7b` for images. They are not called in M1.
- The Codex guest stays offline. It may read only a task-sized export, never the whole library, MemFS, or secrets.
- External writes (mail, Drive, and anything that leaves the machine) still require approval. Isolation between accounts stays as it is.
- Trash, SHA-256 dedupe of bytes, and image metadata stripping are reused from the file library.
- Tests and the public repo use synthetic bytes only.

## What Eagle 4.0 is, for this design

An Eagle library is a directory whose name ends in `.library`. The root `metadata.json` holds the folder tree, smart folders, and tag groups. Each item lives in `images/<id>.info/` with the original, a thumbnail, and its own `metadata.json`. Folders are ids on the item, not directories, so one file can sit in several folders. The on-disk layout used here is the one observed by importers and by the plugin API notes. Eagle does not publish a full format spec. SSIOS tests that layout with a synthetic library in M3. It never writes the user's real Eagle library and does not call `localhost:41595` (Web API v2 needs Build 21 or newer; the reference client is 4.0.0 Build 7).

Import paths in Eagle are drag, paste, a file picker, a browser extension, and a watched local folder. Organization is folders, tags and tag groups, smart folders, notes, and a 0–5 rating. Several libraries can exist side by side.

## How this sits on SSIOS

| Existing piece | Where | Library use |
| --- | --- | --- |
| File library | `accounts/<id>/files/` | M1 copies finalized files into the default personal library and leaves the chat file URLs in place |
| EXIF stripping | PNG, JPEG, GIF, WebP | Same strippers. GPS and device ids never land in the library file |
| 512 KB cap | `files.py` | Chat uploads keep that cap. The library has its own limits, below |
| Notes vault | Markdown clips | Stays separate. A library item may cite a note. An article is not copied into both |
| MemFS | Per-account git repo | Originals never enter it. Extracted facts are an M4 concern |
| LAN UI | `ssios restart --lan` | Same login. Thumbnail and original URLs must load on that origin |

## Disk layout

All of this stays under the state directory.

- `accounts/<id>/library/libraries.json` lists the libraries this account can open.
- `accounts/<id>/library/<library-id>/metadata.json` holds folders, tag groups, tags, and an empty smart-folder list until M2.
- `objects/<sha256>.<ext>` is the immutable original. The same hash is stored once per library.
- `items/<item-id>.json` is the catalog row: identity, folder ids, tags, star rating, source, versions, trash flags.
- `thumbs/<item-id>.png` is a generated tile for types the browser cannot show as an image. Photos use the stripped original.

A shared library, when M5 exists, lives at `shared-libraries/<library-id>/` with an ACL file. M1 does not create that directory.

Item fields used in M1: `id`, `name`, `ext`, `mime`, `size`, `sha256`, `folder_ids`, `tags`, `star`, `annotation`, `source_kind` (`paste`, `drag`, `picker`), `imported_at`, `modified_at`, `captured_at` (a timestamp copied out of the image before the file is stripped, or empty), `versions` (each entry is a hash, size, and time), `trashed`, `trashed_at`, `added_by`. AI fields and `memory.include` are reserved and unused.

## File rules

A file is stored only when it is a finished asset of the kinds listed below:

- Images: PNG, JPEG, GIF, WebP, BMP, TIFF, HEIC/HEIF, AVIF.
- Video: MOV, MP4 and other `ftyp` movies that are not audio-only.
- Audio: WAV, MP3, FLAC, Ogg, M4A.
- Documents: PDF, EPUB, DOCX, PPTX, XLSX.
- Design and fonts: PSD, TTF, OTF, WOFF, WOFF2.

Rejected, with a specific error:

- Empty body.
- Over the limit: 50 MB for images, 100 MB for documents, design files, and fonts, 500 MB for video and audio. The message states that limit.
- Archives (`zip`, `7z`, `rar`, `gz`, `dmg`) and a zip that is not an Office or EPUB package.
- Executables and libraries (MZ, ELF, Mach-O).
- Databases (`SQLite format 3`).
- Hidden and unfinished names: a leading dot, `.DS_Store`, `.part`, `.crdownload`, `.download`, `.tmp`, and any path that contains `.git`.
- Anything else, including source files. They are not asset types Files stores.

Originals are immutable. A file whose name is already in the target folder, or whose bytes are already in the library, is staged and not written until the owner chooses. Keep existing leaves the current item unchanged. Replace with new stores the incoming bytes as the current version and keeps every earlier version. A hash is stored once. Trash hides an item. Permanent delete removes the catalog row and the blob only when no remaining item or version still cites that hash. Permanent delete requires the confirmation word `delete`.

## Milestones

### M1 — library shell

Personal libraries, more than one per account, each with its own folders, tags, ratings, and trash. Migrate existing file-library objects into the default personal library without removing the chat copies. Import by paste, drag, and the file picker through one pipeline: identify, strip image metadata, hash, store, version. Drag accepts files only. A dropped directory is skipped with a localized reason and is not walked. Other files in the same drop are still imported. Dropping onto a folder in the left pane files those items there. A file that matches an existing name in that folder, or matches bytes already stored, asks before anything changes. Keep existing leaves the item alone. Replace with new makes the new bytes the current version and keeps the previous version. Identical bytes say the file is already in the library and the default choice is Keep existing. A multi-file drop can apply that choice to the remaining conflicts. The choice is audited. Three panes: library switcher and trees, thumbnail grid, inspector. Image and PDF tiles. Personal badge: one-person icon and the localized word for personal (`个人` when the account language is Chinese). No AI. No sharing. No smart folders. No watch folder.

### M2 — preview, search, more doors

Smart folders are saved queries with AND, OR, and NOT. They are computed when the list is drawn and never copy a file. Search matches the name, tags, note, description, and OCR text. A watched folder is a local path, off until the owner turns it on, and it refuses a network path. A file is imported only after its size and time match the previous look. The source stays unless the owner asks to delete it. MOV, EPUB, and DOCX previews, the clipper, and Gmail or Drive copies are not in this branch. See `docs/files-open-issues.md`. Embedding scores are a separate bench command, `./scripts/ssios library-embed-bench`, and no model is pinned.

### M3 — Eagle import and visual organize

Read-only copy of a synthetic Eagle 4.x `.library`: folder tree, multi-folder membership, tags, stars, notes, source URL, palettes, and times. Untranslated smart-folder rules are reported, not dropped. Palettes, color and shape filters, and a perceptual hash for near-duplicates. No live mount of the Eagle directory.

### M4 — local analysis and memory

Analysis is on demand, not a background job, unless the library owner turns on automatic analysis (see `docs/background.md`). Text uses `qwen3:14b`. Images use `qwen2.5vl:7b`. The host must be loopback. There is no cloud fallback. The description, tags, and OCR text are stored on the item. A note is saved under `topics/library/<item-id>.md` with the item, the library, who added the file, and the model name. Originals stay out of MemFS. Copying that note into core memory (`human.md`) stages the existing approval and does not write until it is accepted. Vectors, asking about a selection, and movie transcription are not in this branch. Whisper stays off.

### M5 — household sharing

A library other than the default personal one can be shared. The bytes move to `shared-libraries/<library-id>/` with an ACL. Roles are view, add files and tags, and organize folders. The default personal library stays private. A member cannot trash another person's item. Only the owner permanently deletes or changes who is invited. Removing a member or the item forgets that member's `topics/library/<item-id>.md` immediately, so recall stops. Badges: a shared library shows a multi-person icon, the localized word for shared (`共享`), and member initials; a private library shows one person and `个人`; a library shared with you says `由 <name> 共享`. A shared thumbnail shows the adder's initial. Share changes are audited as `library.share` with the member account id and the role. The file name is not in the audit log. An admin still cannot open another account's private library. These role names are the conservative default and can be changed in `library_share.py`.

## Decisions chosen for review

These were open on the design page. M1 uses the conservative side of each. Later milestones can change them.

1. An Eagle library is copied in. SSIOS does not index the original `.library` in place.
2. GPS, camera serial, and owner strings are stripped from the stored image. A capture time, when one is present as `DateTimeOriginal`, is kept only in the item's JSON.
3. Size caps are 50 MB, 100 MB, and 500 MB as above. A watch folder is not part of M1. When it arrives it is a local path, off until the owner turns it on, and it does not accept a LAN or NAS path.
4. M1 does not write MemFS. When extracted facts are written, `topics/library/` may save automatically because they are derived and editable. Copying a fact into core memory still requires the existing approval.
5. Movie transcription stays off.
6. Sharing waits for M5. The planned default is: a member may add files and tags and may organize folders; a member may not delete another person's item; only the owner empties the trash for good or edits the member list.
7. No embedding model is selected. M4 compares small local models on the Mac before one is pinned.
8. A web article remains a Note. The library does not also create a vault note for the same clip.

## Auditing

Library imports, new versions, trash, restore, and permanent delete write `library.import`, `library.version`, `library.trash`, `library.restore`, or `library.purge`. A conflict choice writes `library.keep` (result `kept`) or `library.replace` (result `version`). Replace also writes `library.version` for the new current hash. The extra `reason_code` is `identical` or `name`. The line carries the item id, a byte count, and a result. It does not carry the file name, the note text, or a token. In-library edits are not external writes, so they do not raise an approval card. Permanent delete still requires the confirmation word.

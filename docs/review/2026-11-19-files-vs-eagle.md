# Files compared with Eagle, 2026-11-19

Branch `claude/epic-bardeen-zdd6j2`. Eagle means Eagle 4 as a desktop asset library, in the areas the owner named: import, delete, search, index, tags, and generated tags. Each requirement has its own rubric out of 100. Work on a requirement stops when it scores 90 or more, then the next one starts. Scores are this reviewer's judgment with the evidence named. Synthetic data only.

## Requirements, in priority order

| # | Requirement | Why this order |
| --- | --- | --- |
| R1 | Import | Everything else works on what was imported. A dropped folder is skipped today, which is the largest gap with Eagle |
| R2 | Index | Filters and generated tags need dimensions, colours, and a fast catalog first |
| R3 | Search and filters | The way people find things in Eagle: a filter bar on top of keywords |
| R4 | Tags | Tag manager, groups, autocomplete |
| R5 | Generated tags | Folder auto-tags, rule tags, and model suggestions the owner accepts |
| R6 | Delete and trash | Most of it exists. Empty trash, folder deletion, and retention are missing |

## Rubrics

### R1 Import

| Criterion | Points |
| --- | --- |
| A dropped folder is imported with its subfolders, which become Files folders, as Eagle does | 25 |
| Hidden and system files skipped; unsupported files reported one by one; a limit on how many files and how deep | 10 |
| Import from a web address (an image or PDF link) through the host's fetch rules | 15 |
| Paste an image on the Files page | 10 |
| Width, height, and orientation recorded at import | 15 |
| Choose the target folder and tags for an import | 10 |
| Progress, a summary, and the existing keep-or-replace choice for duplicates | 10 |
| Tests and a browser check | 5 |

### R2 Index

| Criterion | Points |
| --- | --- |
| Width and height for PNG, JPEG, GIF, WebP, BMP, and TIFF read from the file header | 15 |
| Colour palette of up to five colours for images | 20 |
| Older items get the same fields through a re-index, from the command line and in the background | 15 |
| One catalog read per page instead of one per count; a library of 10,000 items lists in under half a second on the CI machine | 25 |
| The catalog survives a restart and notices files changed on disk | 10 |
| Tests | 15 |

### R3 Search and filters

| Criterion | Points |
| --- | --- |
| Keywords over name, tags, note, description, text in the file, source link, and folder names | 15 |
| Filter bar: type, rating, tags (all, any, none), date added, size, dimensions or orientation, colour | 40 |
| Sort by name, date added, date modified, size, rating, and dimensions, both directions | 10 |
| Save the current filters as a smart folder | 10 |
| Fast on 10,000 items | 10 |
| Search by meaning still works with filters | 5 |
| Tests and a browser check | 10 |

### R4 Tags

| Criterion | Points |
| --- | --- |
| Tag manager: every tag with its count; rename, merge, and delete across all items | 30 |
| Tag groups with a colour; a tag can be put in a group | 15 |
| Autocomplete when adding a tag, in the inspector and the batch dialog | 15 |
| Add and remove a tag on one item from the inspector | 10 |
| Several tags at once in the filter | 10 |
| Common tags when several items are selected | 5 |
| Untagged view | 5 |
| Tests | 10 |

### R5 Generated tags

| Criterion | Points |
| --- | --- |
| Folder auto-tags: a folder's tags are added to every item that enters it, on import or on a move | 25 |
| Rule tags at import: type and orientation, and the names of a dropped folder's subfolders if the owner turns that on | 15 |
| Model tags are suggestions until the owner accepts them, one by one or all at once | 25 |
| Background analysis follows the same rule | 10 |
| A new tag that differs from an existing tag only by case or spacing becomes the existing tag | 10 |
| Tests | 15 |

### R6 Delete and trash

| Criterion | Points |
| --- | --- |
| Move to trash and restore, one item or many | 20 |
| Empty the trash with a confirmation | 15 |
| Permanently delete selected items from the trash | 10 |
| Rename and delete a folder; deleting a folder does not delete its items | 20 |
| Remove an item from a folder without deleting it | 5 |
| Empty the trash automatically after a number of days the owner sets, off by default | 10 |
| A stored file is removed only when no item or version still uses it | 10 |
| Tests | 10 |

## Baseline, before this work

| # | Requirement | Score | Evidence and gaps |
| --- | --- | --- | --- |
| R1 | Import | 47 | Files, paste, drag, picker, duplicates with keep or replace, progress per file, a watch folder. A dropped folder is skipped. No import from a link. No dimensions. A drop goes into the open folder but cannot carry tags |
| R2 | Index | 25 | Name, tags, note, description, and OCR text are searchable. No dimensions or colours. Each page reads every item file four times for its counts |
| R3 | Search and filters | 33 | Keywords, meaning search, smart folders with AND, OR, NOT, tag, name, type, and rating. No filter bar, no size, date, dimension, or colour filters. Three sort orders |
| R4 | Tags | 37 | Tags on items, batch add and remove, common tags in a selection, untagged view, tag counts, tag groups imported from Eagle and not shown. No rename, merge, or delete. No autocomplete |
| R5 | Generated tags | 20 | Model analysis adds tags directly, and background analysis can run it. No folder auto-tags or rule tags. No review of model tags |
| R6 | Delete and trash | 55 | Trash and restore for one or many items, permanent delete of one item with the word `delete`, blobs kept while a version uses them. No empty trash, no folder rename or delete, no retention |

## R1 Import: done at 98

| Criterion | Points | Score | Evidence |
| --- | --- | --- | --- |
| Dropped folder keeps its subfolders | 25 | 23 | `import_relative` makes the folder chain with `ensure_folder_path` (case-insensitive reuse, 12 levels). The browser reads a dropped folder through the entry API in batches, and an Import folder button uses `webkitdirectory`. Checked in Chromium with the folder picker and with a stand-in directory tree served in batches. A real Finder drag and Safari are not checked |
| Hidden files skipped, failures reported, limits | 10 | 10 | Hidden names and `.git` are skipped in the browser and refused on the server; archives are reported per file; 5,000 files and 12 levels per drop |
| Import from a link | 15 | 15 | `library_import.import_url` uses the host broker with a 50 MB cap: off until web reads are on, redirects followed and checked, HTML pages and private addresses refused, the link kept as `source_url` |
| Paste on the Files page | 10 | 10 | Existing; pasted and picked files now land in the open folder |
| Width, height, orientation | 15 | 15 | `library_meta` reads PNG, JPEG, GIF, WebP, BMP, TIFF, HEIC, AVIF, and MP4 or MOV (with duration, also when the index is at the end). Shown in the inspector |
| Target folder and tags | 10 | 10 | Drop onto a folder or into the open folder; a "Tags for new files" box applies to drops, the pickers, paste, and links |
| Progress, summary, duplicates | 10 | 10 | Existing per-file progress and keep-or-replace choice |
| Tests and a browser check | 5 | 5 | `tests/test_files_import.py`, including a streamed upload through the real route. The browser check found that the streaming parser dropped the tags field, which is fixed |
| | **100** | **98** | |

## R2 Index: done at 97

Measured on the CI container with 10,000 synthetic items: listing 0.07 s warm, the Files page 0.11 s (it was 1.85 s), the first read after a restart about 0.4 s.

| Criterion | Points | Score | Evidence |
| --- | --- | --- | --- |
| Width and height from the header | 15 | 15 | `library_meta` for PNG, JPEG, GIF, WebP (three kinds), BMP, TIFF (both byte orders), HEIC, and MP4 with duration. `HeaderTest` covers each one |
| Colour palette | 20 | 19 | `library_palette`: up to five colours with their share, transparent pixels left out. A local tool (`sips`, ImageMagick, ffmpeg) makes a 64-pixel BMP for JPEG, HEIC, GIF, WebP, TIFF, and a video frame; without one, PNG and BMP are decoded in Python and a PDF uses its cached preview. Shown as a strip in the inspector; each colour links to a colour filter for R3. Video frames are not covered by a test |
| Re-index of older items | 15 | 15 | `ssios library-reindex --username` (`--library`, `--all`) and the background timer, which works for up to two seconds a tick. An edit made while colours are worked out is kept; a file replaced meanwhile is skipped |
| One catalog read per page; 10,000 items under half a second | 25 | 24 | `library_catalog` keeps the rows in memory and parses only files whose time, size, or inode changed. The page reads it once and filters for the counts and tags. The grid shows 400 cards with a "Show more" link instead of 10,000 at once, which costs a click Eagle does not ask for |
| Survives a restart and notices changed files | 10 | 9 | `catalog-cache.json` holds the rows with their keys; a restart reads that one file. A file changed by a sync tool, a new file, and a removed file are all noticed. On a file system with one-second times, a same-size rewrite within the same second by another program could be missed until the next change |
| Tests | 15 | 15 | `tests/test_files_index.py`, 21 tests, including 10,000 items under the time limits and a check that the page reads the catalog once |
| | **100** | **97** | |

## R3 Search and filters: done at 96

| Criterion | Points | Score | Evidence |
| --- | --- | --- | --- |
| Keywords over every field | 15 | 15 | Name, tags, note, description, text in the file, source link, and folder names. Every word must appear, in any order. Folder names are joined in for one page read and never stored on the item |
| Filter bar | 40 | 36 | `library_filter`: type (several at once), extension, rating and up, tags with all, any, or none, date added (today, 7, 30, 365 days, or a from–to range), size in MB, orientation, minimum width and height, and twelve colour swatches matched against each item's palette (a colour in the inspector also links to its filter). Active filters show as chips with Clear. Eagle applies filters as they are clicked; here the panel has an Apply button, and the colour choice is twelve swatches rather than a free picker (a free colour still works from the inspector link) |
| Sorts, both directions | 10 | 10 | Date added, date modified, name, file size, rating, and pixel count, each ascending or descending. The old `sort=newest` links still work |
| Save the current filters as a smart folder | 10 | 10 | "Save as smart folder" stores the filters and keywords; the folder is worked out again each time, so later files that match appear and trashed ones do not. Written queries (`tag:x AND star>=3`) still work |
| Fast on 10,000 items | 10 | 10 | The filters narrow the rows of the single catalog read. A page with five filters, a keyword, and a sort on 10,000 items stays under half a second in the test |
| Search by meaning with filters | 5 | 5 | Meaning results pass through the same filters |
| Tests and a browser check | 10 | 10 | `tests/test_files_filters.py`, 13 tests including the real GET route with two type boxes ticked. In Chromium: colour and type, then orientation, saved as a smart folder, rating ascending, keyword with tags none; no page errors and no sideways scroll at phone width. The check found the filter panel hidden under the import buttons, which is fixed, and the form now sends only the filters that are set |
| | **100** | **96** | |

The same pass found that the R2 change had dropped the folder from the page's listing (a folder view showed every file). It is fixed and covered by `test_folder_view_still_narrows_and_keeps_its_place`. The browser drop test in `tests/test_library_drop.py` still expected dropped folders to be skipped; it now expects the folder's files in a folder of the same name, which was checked in Chromium.

## R4 Tags: done at 96

| Criterion | Points | Score | Evidence |
| --- | --- | --- | --- |
| Tag manager: counts, rename, merge, delete | 30 | 28 | "Manage tags" lists every tag with its file count and a finder. Rename onto an existing name merges the two (an item with both ends with one, the existing spelling wins); delete takes the tag off every file and asks first. Each change says how many files it touched and is audited. Merging several tags needs one rename each; Eagle can select several and merge at once |
| Tag groups with a colour | 15 | 15 | Add, rename, recolour, and delete groups; a tag is in one group at a time; renamed and merged tags keep their group; deleting a group keeps its tags. Eagle's imported groups show their colours. The sidebar lists tags under their groups with a colour dot |
| Autocomplete | 15 | 14 | One `datalist` of the library's tags serves the inspector, the batch dialog, the filter bar, import, and the manager. It is the browser's own list, which matches from the start of a word only |
| Add and remove a tag on one item | 10 | 10 | The inspector takes several tags at once ("a, b") and each chip has a × button. Chips link to the tag's view |
| Several tags in the filter | 10 | 10 | From R3: all, any, or none of a list |
| Common tags in a selection | 5 | 5 | Existing |
| Untagged view | 5 | 5 | Existing |
| Tests | 10 | 9 | `tests/test_files_tags.py`, 10 tests, including permissions for a viewer of a shared library. Checked in Chromium: group, merge, delete with the confirm dialog (dismiss keeps the tag), inspector add and ×, no sideways scroll at phone width. The check found delete buttons drawn as blank red blocks and the import tag box spilling into the inspector, both fixed |
| | **100** | **96** | |

## R5 Generated tags: done at 95

| Criterion | Points | Score | Evidence |
| --- | --- | --- | --- |
| Folder auto-tags on import and on move | 25 | 24 | `library_autotag`: a folder's tags go on every file imported into it (picker, drop, link, watch folder, a kept-or-replaced duplicate) and every file moved into it from the inspector or a batch. When saving them, "Also add them to the files already here" fills older files. A file moved into a subfolder does not get the parent folder's tags, which Eagle also does not do by default |
| Rule tags | 15 | 15 | Library settings, all off by default: type, orientation (from R2), and a dropped folder's subfolder names. In the account's language (`图片`, `横向`) |
| Model tags are suggestions | 25 | 24 | Analysis writes `suggested_tags`. The inspector shows them dashed with ✓ and × each, plus Accept all and Dismiss all; a dismissed tag is not suggested again. A "Suggested tags" view lists the files waiting and can accept or dismiss them all, after a confirm. Suggestions are not searchable until accepted |
| Background analysis follows the same rule | 10 | 10 | It calls the same analysis; covered by a test |
| Same tag with different case or spacing | 10 | 10 | "Sea View", " sea  view ", "#SEA VIEW" all become the library's existing spelling, in the inspector, on import, in batches, in folder auto-tags, and on accepted suggestions |
| Tests | 15 | 12 | `tests/test_files_autotag.py`, 10 tests, including a Chinese account. Checked in Chromium: the suggested view, accept one, dismiss one, accept all, folder auto-tags saved. Two older tests that expected the model to add tags directly now accept the suggestion first. No browser check of a real model run |
| | **100** | **95** | |

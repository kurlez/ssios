# Files open issues

Items skipped while Files kept moving. Each one says what is needed. The conservative defaults in `docs/design/files.md` stay easy to change.

## Deferred from the design doc

These are specified there and were not part of the smart-folder, watch, analysis, search, and sharing work.

1. Eagle `.library` copy. Needs a synthetic fixture and a confirmed field map. SSIOS does not index a real Eagle directory in place.
2. Rendered previews for MOV, EPUB, and DOCX. The grid still shows a type tile.
3. Sending a clipper image into Files.
4. Read-only copies of Gmail and Drive attachments.
5. Palettes, color and shape filters, and a perceptual hash for near-duplicates.
6. Movie transcription. It stays off.

## Still needs a Mac

7. Embedding scores. M4 adds `./scripts/ssios library-embed-bench`. This environment does not run a local embedding model, so no score is recorded here. Run that command on the Mac. It only talks to loopback Ollama and does not download a model.

# Memory from Files and Notes

Decided with the owner on 2026-11-13. Steps 1 to 3 below are built. Steps 4 and 5 are design only.

The product's memory is Files and Notes joined to chat. The rule is that something the account has not read does not become memory. Having a file is not knowing what it says.

## Three layers

| Layer | What is in it | How chat uses it |
| --- | --- | --- |
| Index | Everything in Files and Notes: names, tags, descriptions from analysis, text read from files, vectors | As pointers only. Chat lists the matching items as cards. It does not answer from their text |
| Read | Items the account has read, with a depth and the signals | Chat may use the text and says how well it was read |
| Memory | Facts confirmed from what was read, with a source | Used as what the account knows |

## What counts as read

There are two depths.

- **Skimmed.** The account went over it quickly.
  - A note in the vault is skimmed from the start. The owner's reason: a clip is made after reading the page once, and a note the account wrote was written by the account.
  - A file is skimmed when the account opens it: in the inspector on the Files page, or with Open in chat.
- **Read.** The account read it with care.
  - Mark as read, on the note page or in the file inspector.
  - A star or a note of the account's own on a file.

Not reading: importing, an Eagle import, analysis by a model, a search hit, and being given access to a shared library.

A reminder is how skimmed becomes read. Remind me to read this, on a note or a file, saves a reminder named `Read carefully: <title>`. Marking the item read completes that reminder.

The ledger is `accounts/<id>/reading.json`: a key, the depth, the signals, and two times. It holds no text. The depth only goes up. The audit log records `reading.mark` with the depth, not the title.

## What is built

1. Analysis stays in the index. `analyze_item` stores the description, tags, and text on the item, where Files search and chat's file search find it. It writes `topics/library/<item>.md` into memory only for an account that has opened the file. Opening an analyzed file for the first time writes the note then. Sharing a library no longer copies notes to the member. `ssios memory-prune-unread --username NAME` takes out notes that the old rule wrote for files the account never opened.
2. The reading ledger and its signals, as listed above.
3. Chat context uses the layers. Core memory and topic notes come first, as before. Then up to two vault notes that match the question, each labelled as `skimmed, not read with care` or `read with care`, and as a note, not confirmed memory. A file the account has not opened never reaches the model's context. It can only appear as a card from the `files` route.

## Still to build

4. **Candidate facts and review.** After the account marks something read, propose one to three facts, each with the quoted line and the source. The account accepts, edits, or rejects. Accepted facts go through the existing memory approval. A review page collects candidates so they do not interrupt chat.
5. **Stale memory.** A fact records the hash of its source. When the file or note changes or is deleted, the fact is marked for review and chat stops using it as current.

Also open:

- Vault notes are matched by words, not by meaning. They are not in the embedding index yet.
- The label reaches the model as text. Nothing checks that the model repeats the caveat. The host could append the source line itself, as it does for connector results.
- Time spent in the viewer is not measured. Opening is the signal.

## Notes follow the Obsidian vault

Notes is the account's Obsidian vault and uses its structure as it is.

- Every `.md` file in every folder is a note. The Notes page shows the vault's folders. `.obsidian`, `.trash`, any other hidden folder, and symbolic links are left out.
- Properties are the YAML block at the top. Tags come from `tags:` and from `#tag` in the text.
- `[[Note]]`, `[[Note|label]]`, `[[Note#heading]]`, and `![[image.png]]` resolve the way Obsidian's shortest path does: beside the current note, then from the vault root, then the one file with that name.
- A clip is saved to `Clippings/`, the folder Obsidian's own clipper uses, with its images in `Clippings/assets/`.
- `ssios vault-set --username NAME --path DIR` points an account's Notes at an existing vault. SSIOS then reads that folder and writes only `Clippings/` and `SSIOS/Tasks.md` in it. It does not write `.obsidian`, and it does not overwrite a `Tasks.md` of the account's own. If the folder is missing, Notes reports that and writes nowhere else.

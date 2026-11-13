# Search by meaning and the host tool loop

Both features are off in the sample config. Neither was run against a real Ollama model for this change. The tests use a stand-in embedder and a scripted model.

## Search by meaning

Keyword search stays as it is. When `[search] embed_model` names an embedding model, Memory and Files also find text that means the same thing with different words, in Chinese or English.

```toml
[search]
embed_model = "bge-m3"
semantic_top_k = 8
semantic_min_score = 0.5
```

Pull the model by hand (`ollama pull bge-m3`). No model is pinned. `./scripts/ssios library-embed-bench` reports which candidates are installed, their dimensions, and the time for one call. For each installed model it also runs six synthetic lookups where the query and the document are in different languages, and prints `top1`, the lowest score of a right answer, the highest score of a wrong one, and a `suggested_min_score` between them. `semantic_min_score` depends on the model, so set it from that report and check a few real searches on the Mac.

Where it is used:

- Files search, on the page and in `search_items`. Keyword matches come first in their usual order. Items found only by meaning follow, closest first.
- Chat memory context. When fewer than three topic notes match by keyword, the closest topic notes fill the remaining places.
- The `memory.search` tool that the guest and the host tool loop call.

Notes in the vault are not indexed yet.

What is stored:

- Files: `index.sqlite3` inside that library's directory, so a shared library has one index and it moves with the library.
- Memory: `accounts/<id>/index/memory.sqlite3`.

A row is a key, a SHA-256 of the text, the model name, and the vector. The text is not stored. Each query compares the index with the documents the account may read at that moment. A file that was forgotten, trashed, or changed is removed or embedded again before ranking. At most 2000 characters of each document are embedded.

Limits:

- The embedding call goes to loopback Ollama only. Another host is refused.
- If the model is missing or slow, the result is the keyword result. The first search after turning this on embeds every document, 16 at a time, for up to 8 seconds. Documents that did not fit are embedded by later searches.
- Vectors are compared in Python. This is sized for a household library, up to about 5000 items per library.
- A vector can leak something about its text. The index file is mode 0600 and sits under the same directory permissions as the data it describes.

## Host tool loop

Today a turn that needs several tools runs Codex in the no-NIC guest. When the guest cannot start, the turn falls back to one route chosen by the rules. With the planner on, that fallback first tries a tool loop on this computer:

```toml
[agent.planner]
enabled = true
```

The local chat model (`[llm] ollama_model`) receives the list of host tools and chooses calls. It is not Codex. It has no shell, no workspace, and no socket. `docs/design/codex-orchestrator.md` rules out starting Codex on the host, and this does not do that. It does mean a model on the host, not in the guest, decides which reads run. That is the reason it is off by default.

- The tools are the ones in `mcp_host.py`: mail, Drive, and calendar reads, memory, tasks, reminders, weather, and one web page. A connector that is not enabled is not offered. Calendar changes are not offered here. They keep the existing calendar path.
- A read runs through the same broker and is audited the same way. A write returns a pending approval card.
- The step limit and time limit are `[agent.codex] step_limit` and `turn_timeout_seconds`.
- Citations are checked the same way as a guest answer. A citation that matches no result is removed.
- A request for file or workspace work is never taken by the loop. It still needs the guest.
- If the model returns no tool call and no answer, or the client does not support tools, the turn uses the single route as before.

The chat model must support Ollama tool calls. `qwen2.5:7b` and `qwen3:14b` advertise them. How well a 7B model plans has not been measured here.

## One web address rule for both paths

Mail, a Drive file, and a web page can contain text that asks the model to open another address, with private text in the query string. The tool session now refuses `web.fetch` and `web.clip` after one of those reads unless the address appears in the user's own message. This applies to the guest and to the host loop. The refusal is audited as `denied`.

## Files from chat

Chat reads the Files catalog on the host. It does not ask a model and does not start the guest.

- A message that names a file and a search word, such as `在我的文件里找Anthropic`, `我的照片在哪`, `find my AMC file`, or `search my files for harbor`, is route `files`. The reply is a list of items with a link to each one on the Files page. When nothing matches, the reply says so.
- A message that only says find, such as `帮我找一下AMC`, checks Files first. When Files has a match it is listed. When it has none, the turn goes where it went before.
- A short message right after one of those, such as `AMC8的练习题`, is treated the same way.
- `标签是数学的文件`, `files tagged work`, `tag:contest`, and `#contest` keep only items that carry that tag.
- A request to edit, delete, rename, create, or run something is not a Files search.

A word matches the file name, tags, note, description, and text read from the file. Any one word is enough, and more matching words rank higher. A word that is exactly a tag counts for more. With `[search] embed_model` set, items close in meaning are added. A file that has not been analyzed can be found only by its name, tags, and note.

The host tool loop has `files.search` and `files.read`. `files.read` returns the stored name, tags, description, and text, never the bytes. Both count as text written by someone else for the web address rule.

Each found item is a card. Open shows an image or a PDF in a viewer over the chat, with links to the Files page and to a new tab. Escape or Close returns to the chat. Other kinds of file have no viewer and only link to the Files page.

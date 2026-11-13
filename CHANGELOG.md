# Changelog

## Unreleased

Not run against a real Ollama model. Both features are off in the sample config. See [docs/search.md](docs/search.md).

- Search by meaning for Files and Memory when `[search] embed_model` is set. Keyword matches stay first. The index stores vectors and hashes, not text, and only loopback Ollama is called.
- A host tool loop for a multi-tool turn when the Codex guest cannot start (`[agent.planner] enabled`). Same host tools, same audit, and a write is still an approval card.
- After mail, Drive, or a web page is read in a turn, `web.fetch` and `web.clip` open only an address the user typed. This applies to the guest too.
- `library-embed-bench` also probes `bge-m3` and reports six cross-language lookups with a suggested `semantic_min_score`.
- `backup`, `backup-verify`, and `restore`. The archive carries a manifest of hashes, is not encrypted, and leaves out `run/`, `cache/`, and keychain items. Restore writes only into a new, empty directory.
- A daily brief built from stored reminders, tasks, and a calendar read, with no model. Off until the account turns it on in Settings.
- A library owner can have new files analyzed in the background, one file per timer pass. Off by default.
- `launchd-plist` prints a LaunchAgent that starts the UI at sign-in. It installs nothing. See [docs/background.md](docs/background.md).
- Opening one Files item reads its own row instead of the whole catalog (84 ms to under 1 ms with 3000 synthetic items on the CI machine). Listing 3000 items takes about 0.1 s, so the catalog stays as JSON files.
- Chat finds things in Files. `在我的文件里找Anthropic`, `find my AMC file`, and `标签是数学的文件` list matching items with links, from the catalog and with no model. A bare `帮我找一下AMC`, or a short follow-up to it, lists Files matches first when there are any. `files.search` and `files.read` are host tools.
- The Files sidebar has a Tags section with a count per tag. It lists tags from analysis and batch edits, which the page built before and never showed. The search box takes `#tag` and `tag:name`.
- The chat box sends on Ctrl+Enter, or Command+Enter. Enter is a new line.
- A weather timeout is retried once and reports `weather_unreachable` with the service name, not the hint for a blocked web page.
- A PDF thumbnail waits at most 6 seconds for all drawing tools together, and a PDF that could not be drawn is not tried again on every request. On a Mac a damaged PDF could hold the request for 30 seconds.
- Three tests no longer depend on the machine: one assumed krunvm was not installed, and two assumed the temp directory had no linked parent, which is not true on macOS.
- [docs/release-checklist.md](docs/release-checklist.md) lists what has to be run on the Mac before a tag.

## 0.3.0 — 2026-11-13

Files and the Notes/Tasks fixes. The package version is `0.3.0` (`pyproject.toml` and `ssios.__version__`). Synthetic data only. CI ran 326 tests: 319 passed, 7 skipped. The scorecard mean is 90 / 100.

- Files library at `/ui/library`: drag in files (a dropped folder is skipped), keep or replace on a name or byte conflict, folders, tags, and inline rename. The same bytes are stored once.
- Smart folders are saved queries and do not copy files. A watched folder is a local path, off until the owner turns it on, and it refuses a network path.
- On-demand local analysis (description, tags, OCR) writes `topics/library/<item-id>.md`. Copying a fact into core memory still waits for approval.
- Search covers the name, tags, note, description, and OCR text. `./scripts/ssios library-embed-bench` probes loopback Ollama and does not pin a model.
- Household sharing with view, add, and organize roles. The owner is the only person who can change sharing or permanently delete. A removed member loses recall immediately.
- Multi-select (click, Command/Ctrl, Shift, marquee) and a batch bar: tag, move, trash, restore, download a zip, and analyze. Permanent delete still requires the word `delete`.
- Tasks is a checklist. Checking an item goes through approval and the audit log. A one-shot chat request is not turned into a goal.
- Notes clip errors and a failed guest mail read stay on the host and use the language of the latest message.
- Codex stays the main chat route inside the no-NIC guest. Explicit commands stay on the host.

## 0.2.0 — 2026-11-12

Household chat release. The package version is `0.2.0` (`pyproject.toml` and `ssios.__version__`). Synthetic data only. The scorecard mean is 90 / 100.

- Chat UI: a three-pane page (icon rail, thread list, composer), local Markdown and KaTeX, per-account history, and an approval card that stays in the transcript.
- Household accounts: each account has its own chat, memory, files, and tokens. An admin cannot read another account's mail or memory.
- Gmail, Drive, and Calendar connectors. Reads run on this computer. Calendar writes still wait for one approval. A refresh `invalid_grant` clears that service's token and the card says Needs reconnect.
- Codex on the main chat route inside the no-NIC `macos-vz` guest. Explicit commands stay on the host. Choosing VM still sends that turn to the guest. A story or other no-tool request is answered in the final message. Isolation checks sit under Details on the Checks chip and are written to the audit log.
- Substack and WeChat article fetch on the host, with a site session for a paid page and a separate reply for a paywall, a verification check, or a deleted article.
- Clipboard paste of screenshots, including Snipaste BMP and empty types, shown in the composer and stored once. An image is read by `qwen2.5vl:7b`.
- Weather skill on this computer, with place disambiguation, a saved default city, and replies in the language of the latest message.
- Per-account memory for a chosen city, stated preferences, corrections, and language or units from Settings. A name the user asks to remember still waits for approval.
- Router: rules first, then an optional small classifier (`qwen3:1.7b` or `qwen3:4b`) that only labels the turn. A timeout or a low score stays on local chat.
- `model-bench` and `router-bench`. `router-bench --rules-only` runs in CI and does not call Ollama.

## 0.1.0 — 2026-11-10

First prototype release of SSIOS, a local personal Agent OS. The package version is `0.1.0` (`pyproject.toml` and `ssios.__version__`).

- Letta MemFS-compatible Git Markdown memory (`letta-memfs-compat`), plus an optional pinned Letta Code 0.34.45 local backend (`letta-memfs-upstream`). Memory commits wait for human approval.
- Approval flow bound to the action, payload hash, expected revision, and a one-time nonce, with expiry and a loopback-only UI.
- Local models through Ollama, with no cloud fallback. The dev default stays the deterministic mock.
- Cloud Hypervisor microVM on Linux for the summary worker and for Codex, with no guest NIC.
- vfkit `macos-vz` Codex guest with no NIC. Commit `fe9cbd8` was verified on an Apple M4 Mac mini by `scripts/macos-vz-codex-smoke.sh`: `awaiting_approval`, `isolated` true, the workspace marker written, the sandbox denial recorded (`sandbox_denied` true), the outside write refused, and the only NIC `lo`.

### Known limitations

- No TEE, measured boot, or TPM-sealed vault. Disk encryption is an operator prerequisite (FileVault or LUKS), not a feature of this release.
- macOS is verified only for that `macos-vz` Codex smoke on the M4. The krunvm summary path was not re-checked for this tag.
- Gmail and Drive connectors are design notes only. There is no guest network broker.
- A dev-guard task is an in-process allowlist, not a virtual machine.

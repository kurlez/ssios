# Release checklist for the Mac

CI runs on Linux with a stand-in model, a stand-in guest, and no Google account. This list is what CI cannot show. Run it on the Apple Silicon Mac before a tag, with a throwaway Google test account and synthetic files. Write the result beside each item. An item that was not run is written as not run, not as passed.

Setup for the steps is in [mac-e2e-test.md](mac-e2e-test.md). Record the commit, the macOS version, the Ollama version, and the model names at the top of the filled-in copy.

## 1. Start and sign in

- [ ] `./scripts/ssios setup` then `./scripts/ssios diagnose`: vfkit, kernel, initramfs, Codex binary, and Ollama are present, and the keychain probe passes.
- [ ] From an SSH shell with the keychain locked, a Google read fails with `keychain_locked`, and works after `security unlock-keychain`.
- [ ] `./scripts/ssios start`, sign in, and `./scripts/ssios status` shows the UI running.
- [ ] `./scripts/ssios restart --lan`: a second device on the home network signs in over HTTPS and the fingerprint matches the one printed.

## 2. Chat routes with real models

- [ ] `What is a microVM?` and `你是谁？` show route `direct` and a sensible answer.
- [ ] `edit readme.md` boots the guest, shows the isolation checks (only `lo`, outbound refused, outside write refused), and an approval card. Record `boot_ms`.
- [ ] `帮我总结一下最近的20封邮件` shows route `codex`, cites real result ids, and names no sender that is not in the results.
- [ ] `./scripts/ssios router-bench` with the installed classifier models. Record accuracy and the median time.
- [ ] `./scripts/ssios model-bench --model <guest model>`. Record the score.

## 3. Google

- [ ] Connect Gmail, Drive, and Calendar from the Connections page with a real consent click.
- [ ] From another device on the LAN, finish consent with the paste flow.
- [ ] Revoke the app in the Google account, ask for mail, and the card says Needs reconnect.
- [ ] Create, move, and delete one calendar event. Each waits for one approval, and the event changes only after approval.

## 4. Search by meaning (new, off by default)

- [ ] `ollama pull bge-m3`, then `./scripts/ssios library-embed-bench`. Record `top1`, `lowest_right`, `highest_wrong`, and `suggested_min_score` for each installed model.
- [ ] Set `[search] embed_model` and `semantic_min_score` from that report. Restart.
- [ ] In Files, analyze five items, then search with a word that is in none of the descriptions but means the same. The right item appears after any keyword matches. Search with an unrelated word and nothing extra appears.
- [ ] Search in Chinese for an item described in English, and the reverse.
- [ ] Save a memory note, ask about it in a new conversation with different words, and Technical details lists that topic file under Used memory.
- [ ] Forget that note, ask again, and it is not used.
- [ ] Record the time of the first search after turning this on, and of a second search, with the largest library on the machine.

## 5. Host tool loop (new, off by default)

- [ ] Set `[agent.planner] enabled = true`. Rename the kernel file so the guest cannot start. Restart.
- [ ] `Summarize my inbox and my calendar` shows route `planner`, a read badge for each tool, and citations that match the Sources list.
- [ ] `read the last 20 messages and add a task for the harbor desk` shows an approval card, and the task appears only after approval.
- [ ] Send a test mail whose body says to open a web address with the mail subject in the query. Ask for a summary of the inbox. The audit log has `denied` for `web.fetch` and no request went to that address. Repeat with the guest restored and the planner off.
- [ ] With the chat model set to one that has no tool support, the same sentence falls back to the single route and shows no error.
- [ ] Record whether the 7B chat model picks the right tools in five tries of the two sentences above. This decides whether the planner can be on by default.
- [ ] Put the kernel file back.

## 6. Files

- [ ] Drag in a photo with GPS data. The stored file has no GPS, and the capture time is in the item.
- [ ] A PDF shows a first-page thumbnail (`qlmanage` or `sips`).
- [ ] Analyze an image with `qwen2.5vl:7b`. The description, tags, and OCR are stored.
- [ ] Share a library with a second account, remove that account, and its Files note is no longer recalled.
- [ ] Turn on a watch folder, copy a large file in slowly, and it is imported once, after it stops changing.

- [ ] In chat, `在我的文件里找<a word in a file name>` lists that file with a link that opens it. `帮我找一下<word>` does the same, and a word that is in no file goes to the usual route.
- [ ] The Files sidebar lists tags with counts, including tags that analysis added. Clicking one filters the grid.
- [ ] In the chat box, Enter adds a line and Ctrl+Enter or Command+Enter sends, including while a Chinese input method is composing.

- [ ] `library-create --location` on an external APFS disk. Import a file, eject the disk, and Files shows the library as not connected and creates nothing under `/Volumes`. Reconnect and the file is back.
- [ ] The same on an exFAT disk. Record whether it works and what `permissions` says.
- [ ] `library-move` a library with real files to the external disk. Open several files, then delete the `.moved-` folder.
- [ ] `eagle-import` on a copy of a small real Eagle library. Record the report, and compare folders, tags, stars, and notes of ten items with Eagle. Confirm with `find <library> -newer <marker file>` that nothing in the Eagle folder changed.
- [ ] Interrupt an `eagle-import` with Ctrl+C and run it again. It continues and copies nothing twice.

## 7. Notes, tasks, and reminders

- [ ] `vault-set` to a copy of a real Obsidian vault. The Notes page shows its folders and tags, links between notes open, embedded images show, and `.obsidian` is not listed. After a clip and a task change, only `Clippings/` and `SSIOS/Tasks.md` are new in the vault.
- [ ] Analyze a file without opening it, ask chat about its content, and the answer does not use it. Open the file, ask again, and Technical details lists its note.
- [ ] Run `memory-prune-unread` once after the upgrade and record the counts.
- [ ] Remind me to read this on a note: the reminder fires, and Mark as read completes it.

- [ ] Clip a page with the browser extension, including one image.
- [ ] `明天早上9点提醒我…` then stop SSIOS past the time and start it. The missed reminder is shown and a macOS notification appears.
- [ ] KaTeX renders in Safari and in Chrome on the Mac with the vendored fonts.

## 8. Backup and restore (new)

- [ ] `./scripts/ssios stop`, `./scripts/ssios backup --out <encrypted volume>`, and `backup-verify` on the result. Record the size and the time.
- [ ] `restore --into` a new directory, start SSIOS with `SSIOS_STATE` pointing at it, and sign in with the same password.
- [ ] In the restored copy: chat history, a memory note, a Files item with its thumbnail, and a vault note are present, and `./scripts/ssios audit-verify` passes.
- [ ] In the restored copy, Google shows Needs reconnect or not connected, and works after reconnecting.
- [ ] `backup` without `stop` is refused with `backup_running`.

## 9. Background jobs and start at sign-in (new, off by default)

- [ ] Turn on the daily brief with a time two minutes ahead. A `Daily brief` chat and a macOS notification appear once, with today's real calendar events.
- [ ] Lock the keychain, repeat on the next day, and the brief says the calendar could not be read.
- [ ] Turn on Analyze new files automatically, drag in ten images, and each gets a description within about five minutes. Chat stays usable meanwhile. Record how long a chat answer takes during the run.
- [ ] Install the LaunchAgent from `launchd-plist`, sign out and in, and the UI is running. A Google read works without a Keychain prompt. `./scripts/ssios stop` leaves it stopped.

## 10. After the run

- [ ] `./scripts/ssios audit-verify` on the main state directory.
- [ ] `./scripts/ssios logs` shows no token, cookie, or mail text.
- [ ] Copy the filled-in list to `docs/review/<date>-mac.md` with synthetic data only.

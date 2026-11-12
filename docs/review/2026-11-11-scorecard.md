# SSIOS scorecard, 2026-11-11

Branch `cursor/ssios-chat-ui-2e2e`. Scores are out of 100 for each requirement group. The total is the unweighted mean. Synthetic data only.

## P0 Correctness

| Requirement | Status | Evidence | Score |
| --- | --- | --- | --- |
| Personal-data answers come from tool results | Done for mail, calendar, and Drive reads. A mail report is kept when the model returns text, a citation to a missing item is removed, and an empty or failed call falls back to the list | `tests/test_chat_accounts.py` `test_failed_or_empty_gmail_never_invents_mail`; `tests/test_read_report.py` | 92 |
| Failed or empty connector reads do not call the model | Done | Same test: a 403 and an empty inbox leave `complete_chat` uncalled | 95 |
| Summaries that name a sender, subject, or date outside the results are dropped | A grounded reply still drops names that are not in the tool result. A mail report is kept when the model returns text; a citation to a missing item is removed, and an empty or failed model call falls back to the list with the reason | `_mentions_are_in_results`; `tests/test_read_report.py` | 90 |
| Host clock in every prompt and deterministic date answers | Done earlier | `test_pure_date_questions_use_the_host_clock` | 94 |
| Relative times stored as absolute timestamps | Done for one-off and weekly reminders, and for dated memory notes | `test_reminder_uses_an_absolute_time_across_midnight`; `tests/test_reminders.py` stores `2026-11-11T09:00:00-08:00` and `weekly:mon` | 90 |
| The model cannot claim a save that did not happen | A reminder and an auto memory save both reply from the store write. The mock sentence is absent | `test_reminder_uses_an_absolute_time_across_midnight`; `tests/test_memory_panel.py` | 90 |

## P0 Connectors

| Requirement | Status | Evidence | Score |
| --- | --- | --- | --- |
| Chinese and relative calendar, Gmail, and Drive intents | Done earlier | Calendar, Gmail, and Drive tests in `test_chat_accounts.py` | 90 |
| Calendar defaults to primary | Done earlier | `test_calendar_phrases_survive_a_broken_model` | 92 |
| Gmail inbox defaults, metadata, sender counts | Done earlier | `test_chinese_mail_and_drive_reads_list_results_without_approval` | 90 |
| Drive root, folder name, and keyword | Done earlier | `test_mail_and_drive_plans_do_not_copy_the_sentence_into_the_query` | 90 |
| `connector_api_disabled` names the API and console link | Done | `test_refresh_invalid_grant_asks_for_reconnect_and_disabled_api_names_the_reason` | 90 |
| Other permission denials stay `connector_scope_missing` | Done earlier | `test_calendar_identity_stays_inside_the_granted_scopes` | 88 |
| `invalid_grant` on refresh clears the token and shows Needs reconnect | Done | Same refresh test. Connections page renders Needs reconnect. Screenshot `needs-reconnect-1280.png` | 90 |
| Stale consent hint, scope log, setup checklist, API docs | Done | `docs/google-connector.md`, existing scope log tests | 88 |

## P0 Memory

| Requirement | Status | Evidence | Score |
| --- | --- | --- | --- |
| One MemFS repo per account, shared by conversations and routes | Done | `test_saved_name_is_recalled_in_a_new_conversation` checks a new conversation, git log, and the other account | 88 |
| Core blocks including human.md, plus topic keyword retrieval | Done in `memory_context` | The recall test sees `Used memory: human.md`. `test_a_saved_topic_is_loaded_by_keyword` loads a topic note | 90 |
| Explicit facts become one approval | Regex facts plus a JSON extract. A value is kept only when the user stated it. `记住我叫Kent` writes `human.md`, including when there is no space after 我叫 | `tests/test_memory_panel.py`; `test_chinese_name_is_saved_to_human_and_recalled` | 90 |
| Auto-commit with undo | Default remains approve-each. Auto writes and undo restores the previous file | `test_auto_saves_and_undo_restores_without_logging_the_fact` | 90 |
| Chinese remember and forget cues, distinct note files | Done | `test_memory_is_not_written_until_the_owner_approves` rejects `topics/chat/note.md` | 88 |
| Memory panel lists, searches, renders, and shows history and trust | Edit and forget are on the page. Approve-each stays the default | `test_panel_edit_and_forget` | 90 |
| `letta-memfs-compat` explained | Done | `docs/memfs.md` and the README | 92 |

## P1 UX and docs

| Requirement | Status | Evidence | Score |
| --- | --- | --- | --- |
| Result cards, per-query time range, technical details | The chat page is three panes: an icon rail, thread list, and a collapsible info pane. Overview lists scheduled tasks and an activity timeline built from the audit log | `tests/test_workspace_ui.py`; screenshots `chat-after-1280.png` and `chat-after-mobile.png` | 90 |
| Sanitized Markdown and KaTeX, local files only | Vendored renderer. The chat screenshot shows a heading, a list, `$12.50`, the formula, and escaped script text | `tests/test_render.py`; screenshot `chat-render-1280.png` | 90 |
| Approval page shows status, time, outcome, and a conversation link. Approve disables after click. Expired memory approvals can be retried | Retry stages a new approval. The expired one does not write | `test_expired_memory_approval_can_be_retried` | 90 |
| README matches `./scripts/ssios` and a test checks the commands | Done | `tests/test_readme_commands.py` | 90 |

## P2 Skills, vault, tasks

| Requirement | Status | Evidence | Score |
| --- | --- | --- | --- |
| Weather and web fetch on the host, SSRF checks, untrusted pages | A free-form place stays on the weather skill. A geocode miss is city_not_found, and the local model is not asked for a live forecast | `tests/test_skills.py`; `tests/test_weather_followup.py` | 86 |
| Obsidian vault clips, assets, and Notes view | A clip keeps the image, strips script, and leaves page instructions as text. Notes opens for a household account. An empty clip, an oversize clip, and a rejected vault path each have their own message | `test_a_clip_saves_the_image_and_keeps_page_text_untrusted`; `tests/test_notes_clip.py`; screenshots `notes-vault-1280.png` and `note-clip-1280.png` | 90 |
| Tasks with evidence, undo, and a Tasks.md mirror. Reminders with absolute time, recurrence, restart survival, and chat notification | A tool result completes the matching goal and undo reopens it. A page read does not. Pages poll notices that remain after restart. The Tasks list is a checkbox list from the stored goals. Checking a box stages an approval | `tests/test_goals.py` `GoalChatLoopTest`; `tests/test_reminders.py` `NoticeBadgeTest`; `tests/test_task_checklist.py`; screenshot `tasks-checklist-1280.png` | 90 |

## Total

Mean of the 28 rows above: **90 / 100** (2523 / 28).

`PYTHONPATH=src:tests python3 -m unittest discover -s tests` — 308 ran, 301 passed, 7 skipped.

Screenshots, synthetic data only: `tasks-badge-1280.png`, `tasks-checklist-1280.png`, `chat-render-1280.png`, `notes-vault-1280.png`, `note-clip-1280.png`, `needs-reconnect-1280.png`.

Still for a Mac: Keychain unlock, a real Google reconnect click, a real macOS notification, and KaTeX font files in the browser on that machine. This VM checked the Linux UI, the poll banner, and the vendored renderer.

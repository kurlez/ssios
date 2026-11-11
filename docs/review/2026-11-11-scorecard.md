# SSIOS scorecard, 2026-11-11

Branch `cursor/ssios-chat-ui-2e2e`. Scores are out of 100 for each requirement group. The total is the unweighted mean. Synthetic data only.

## P0 Correctness

| Requirement | Status | Evidence | Score |
| --- | --- | --- | --- |
| Personal-data answers come from tool results | Done for mail, calendar, and Drive reads | `tests/test_chat_accounts.py` `test_failed_or_empty_gmail_never_invents_mail` | 92 |
| Failed or empty connector reads do not call the model | Done | Same test: a 403 and an empty inbox leave `complete_chat` uncalled | 95 |
| Summaries that name a sender, subject, or date outside the results are dropped | Done | `_mentions_are_in_results`; the same test drops Jane Doe / 2023 | 90 |
| Host clock in every prompt and deterministic date answers | Done earlier | `test_pure_date_questions_use_the_host_clock` | 94 |
| Relative times stored as absolute timestamps | Done for reminders and for dated memory notes | `test_reminder_uses_an_absolute_time_across_midnight` | 86 |
| The model cannot claim a save that did not happen | Reminders skip the model. Notes stay staged until approval | Reminder test asserts the model sentence is absent | 84 |

## P0 Connectors

| Requirement | Status | Evidence | Score |
| --- | --- | --- | --- |
| Chinese and relative calendar, Gmail, and Drive intents | Done earlier | Calendar, Gmail, and Drive tests in `test_chat_accounts.py` | 90 |
| Calendar defaults to primary | Done earlier | `test_calendar_phrases_survive_a_broken_model` | 92 |
| Gmail inbox defaults, metadata, sender counts | Done earlier | `test_chinese_mail_and_drive_reads_list_results_without_approval` | 90 |
| Drive root, folder name, and keyword | Done earlier | `test_mail_and_drive_plans_do_not_copy_the_sentence_into_the_query` | 90 |
| `connector_api_disabled` names the API and console link | Done | `test_refresh_invalid_grant_asks_for_reconnect_and_disabled_api_names_the_reason` | 90 |
| Other permission denials stay `connector_scope_missing` | Done earlier | `test_calendar_identity_stays_inside_the_granted_scopes` | 88 |
| `invalid_grant` on refresh clears the token and shows Needs reconnect | Done | Same refresh test. Connections page renders Reconnect | 86 |
| Stale consent hint, scope log, setup checklist, API docs | Done | `docs/google-connector.md`, existing scope log tests | 88 |

## P0 Memory

| Requirement | Status | Evidence | Score |
| --- | --- | --- | --- |
| One MemFS repo per account, shared by conversations and routes | Done | `test_saved_name_is_recalled_in_a_new_conversation` checks a new conversation, git log, and the other account | 88 |
| Core blocks including human.md, plus topic keyword retrieval | Done in `memory_context` | The recall test sees `Used memory: human.md` and `MEMORY-CONTEXT` | 84 |
| Explicit facts become one approval | Done for name, preference, and home. Not a model JSON extractor | `explicit_facts` and the recall test | 74 |
| Auto-commit with undo | Not wired. The preference file helper exists | `account_memory.write_mode` | 40 |
| Chinese remember and forget cues, distinct note files | Done | `test_memory_is_not_written_until_the_owner_approves` rejects `topics/chat/note.md` | 88 |
| Memory panel lists, searches, renders, and shows history and trust | Partial. Edit and forget forms are not on the page | Memory page in `ui.py`; history assertion in the note test | 70 |
| `letta-memfs-compat` explained | Done | `docs/memfs.md` and the README | 92 |

## P1 UX and docs

| Requirement | Status | Evidence | Score |
| --- | --- | --- | --- |
| Result cards, per-query time range, technical details | Done earlier | Calendar card tests | 88 |
| Sanitized Markdown and KaTeX, local files only | Renderer and KaTeX are vendored and the chat page loads them | `tests/test_render.py` | 80 |
| Approval page shows status, time, outcome, and a conversation link. Approve disables after click. Expired memory approvals can be retried | Status page and disable script are in. Retry is implemented for memory and not covered by a test | `test_memory_is_not_written_until_the_owner_approves` second approve | 82 |
| README matches `./scripts/ssios` and a test checks the commands | Done | `tests/test_readme_commands.py` | 90 |

## P2 Skills, vault, tasks

| Requirement | Status | Evidence | Score |
| --- | --- | --- | --- |
| Weather and web fetch on the host, SSRF checks, untrusted pages | Done earlier | `tests/test_skills.py` | 86 |
| Obsidian vault clips, assets, and Notes view | Routes exist from an earlier commit. Image and injection tests are still thin | `src/ssios/vault.py` | 58 |
| Tasks with evidence, undo, and a Tasks.md mirror. Reminders with absolute time, recurrence, restart survival, and chat notification | Absolute reminders, a Tasks.md mirror, and a daily flag exist. There is no background notifier and no evidence-based task completion | `test_reminder_uses_an_absolute_time_across_midnight` | 48 |

## Total

Mean of the 24 rows above: **82 / 100**.

This is below 90. The next gaps are auto-commit with undo, memory edit and forget on the panel, an expired-approval retry test, a real task completion loop, and a Mac check of KaTeX plus the reconnect badge.

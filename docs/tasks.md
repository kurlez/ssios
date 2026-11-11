# Goals and tasks

Each account has its own goal list at `<state>/accounts/<account-id>/goals.jsonl`. Guest task records in `tasks.json` are separate. They are the virtual-machine jobs, not these goals.

A goal has an id, title, description, source conversation and message, `created_at`, `updated_at`, status (`open`, `in_progress`, `done`, or `dropped`), an optional due time, and evidence when it is done. Due times use the host clock and the account time zone. The stored value is an ISO timestamp with the offset, not the word "tomorrow".

After a chat turn, SSIOS looks only at the user's message. A local model may return JSON (`temperature` 0). A proposal is kept only when the title appears in that message and it is an explicit ask, such as "please ...", "I need to ...", or "帮我 ...". Hello, thanks, and questions are not goals. A new goal that is too similar to an open one is not added again.

A goal becomes `done` only when the same turn has a successful tool result or a host answer, and the user's message matches the goal. A sentence from the model, or text inside mail or a web page, is not evidence. Those pages are untrusted and are not passed to the tracker.

The default is to save the change and show `Added goal:` or `Completed:` with Undo. On the Tasks page the account can choose `auto`, `suggest`, or `off`. Suggest shows the goal and waits for Add. The audit log stores the goal id and the status. It does not store the title or the message.

If the account vault directory exists, the same list is written to `Tasks.md` there as a Markdown checklist. Dropped goals are omitted.

The Tasks page lists Open and Done, filters by status, and can add, complete, drop, or undo a goal. Source opens the conversation that created it.

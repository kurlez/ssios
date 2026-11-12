# Goals and tasks

Each account has its own goal list at `<state>/accounts/<account-id>/goals.jsonl`. Guest task records in `tasks.json` are separate. They are the virtual-machine jobs, not these goals.

A goal has an id, title, description, source conversation and message, `created_at`, `updated_at`, status (`open`, `in_progress`, `done`, or `dropped`), an optional due time, and evidence when it is done. Due times use the host clock and the account time zone. The stored value is an ISO timestamp with the offset, not the word "tomorrow".

After a chat turn, SSIOS looks only at the user's message. A local model may return JSON (`temperature` 0). A proposal is kept only when the title appears in that message and it is an explicit ask, such as "please ...", "I need to ...", or "帮我 ...". Hello, thanks, and questions are not goals. A new goal that is too similar to an open one is not added again.

A goal becomes `done` only when the same turn has a successful tool result or a host answer, and the user's message matches the goal. A sentence from the model, or text inside mail or a web page, is not evidence. Those pages are untrusted and are not passed to the tracker.

The default is to save the change and show `Added goal:` or `Completed:` with Undo. On the Tasks page the account can choose `auto`, `suggest`, or `off`. Suggest shows the goal and waits for Add. The audit log stores the goal id and the status. It does not store the title or the message.

If the account vault directory exists, the same list is written to `Tasks.md` there as a Markdown checklist. Dropped goals are omitted.

The Tasks page lists Open and Done, filters by status, and can add, complete, drop, or undo a goal. Source opens the conversation that created it. A Reminders section on the same page lists open reminders and any that were missed while SSIOS was stopped.

## Task commands

These sentences are handled in code, in Chinese or English. They do not go to the local model. They still work when goal updates are set to `suggest` or `off`. Auto-derived goals, above, are separate and still follow that setting.

- `add a task …` or `帮我记个任务…` adds a task.
- `list my tasks` or `我的任务` lists open tasks.
- `把X标记完成` or `mark X done` completes the matching open task.
- `drop the task X` drops it.
- `rename the task X to Y` or `把任务X改成Y` changes the title.
- `set the task X due …` or `把X任务改到…` sets an absolute due time.

A sentence that is only a question, or text quoted from a page, is not a task command. Completing a task this way is the account speaking. A model reply still cannot mark a task done.

## Reminders

A reminder is a time trigger with a message. It can point at a task, and a task can have several reminders. A reminder can also stand alone. The due time is computed from the host clock and the account time zone and stored as an absolute timestamp. `every Monday` and `每天` repeat. A weekly reminder with no clock uses 09:00.

Chat:

- `明天早上9点提醒我…` or `remind me …` saves a reminder and confirms the absolute time.
- `every Monday remind me …` repeats weekly.
- `list my reminders` or `我的提醒` lists open reminders.
- `cancel the … reminder` or `取消提醒` cancels a pending reminder.
- `snooze the … reminder for 30 minutes` moves the next time. With no duration, snooze is one hour.

Completing or dropping a task cancels its pending reminders. A reminder firing does not complete the task. One-off reminders become `fired`. Repeating ones move to the next day or the next week.

The host service checks reminders when it starts, on a timer while it is running, and when the Tasks page is opened. A reminder that was due while SSIOS was stopped is shown there on the next start. Signed-in pages poll `/ui/notices` and show the count beside Tasks. On macOS, the same moment can also raise a notification. On other systems the in-app notice is the delivery.

After a reminder is saved, SSIOS can offer a Google Calendar event. That offer is a write. It becomes an approval card, and nothing is created until the account approves it. If Calendar is not connected, SSIOS says so and does not invent an event.

The audit log records reminder and task ids and statuses. It does not record the message.

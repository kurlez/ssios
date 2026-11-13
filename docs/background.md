# Background jobs and start at sign-in

The host already has a 30-second timer for reminders. Two more jobs run on it. Both are off until an account turns them on. Neither was run on the Mac for this change.

## Daily brief

Settings has a Daily brief section with a switch and a time such as `08:00`, in the account's time zone. At or after that time, once per day, SSIOS creates a chat named `Daily brief <date>` and shows a notice beside Tasks. On macOS the notice is also a notification.

The brief lists:

- reminders due today,
- open tasks that are due today or overdue, and how many tasks are open,
- today's calendar events when Calendar is connected.

It is built from the stored reminders and tasks and one calendar read. No model writes it. If the calendar read fails, for example because the keychain is locked, the brief says the calendar could not be read and still lists the rest. If SSIOS was stopped at the chosen time, the brief is posted at the next start on the same day. A day that was missed entirely is not posted later.

The setting is `accounts/<id>/brief.json`. The audit log records `brief.post` with the conversation id, not the text.

## Analyze new files

`docs/design/files.md` made analysis on demand only. That is still the default. The owner of a library can turn on Analyze new files automatically under the library settings on the Files page.

With the switch on, each timer pass analyzes at most one file that has no analysis yet, with the same local models and the same code as the Analyze button. So a large import is described at about two files a minute and does not hold the model for long. It runs only when `[llm] provider` is `ollama`.

A file whose analysis fails is recorded in `auto-analyze.json` in the library directory and is not tried again on every pass. Saving the switch again clears that list. Only the owner can change the switch, and the change is audited as `library.auto_analyze`.

Mail is not read in the background, and nothing is written to core memory without the existing approval.

## Start at sign-in on macOS

`./scripts/ssios start` uses `nohup` and does not install anything. To start the UI when you sign in at the Mac, print a LaunchAgent and install it yourself:

```bash
./scripts/ssios launchd-plist > "$HOME/Library/LaunchAgents/local.ssios.ui.plist"
```

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.ssios.ui.plist"
```

The agent runs `./scripts/ssios fg` with this state directory and config. It is limited to the Aqua session, which is the desktop session after you sign in, so the login keychain is unlocked. It does not start at boot before sign-in, and it does not start for an SSH-only login. `KeepAlive` is off: `./scripts/ssios stop` stops it and it stays stopped until the next sign-in or `launchctl kickstart`. `status`, `logs`, and `stop` work as before because `fg` writes the same pid file.

To remove it:

```bash
launchctl bootout "gui/$(id -u)/local.ssios.ui"
```

The plist was checked for its keys in the tests. It has not been loaded on a Mac. Whether Keychain reads work from this agent without a prompt is an item on the release checklist.

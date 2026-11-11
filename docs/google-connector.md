# Gmail, Drive, and Calendar

This is the host-side connector for workstream B in `docs/requirements/chat-ui-accounts-google.md`. The chat UI imports this module. The Mac walkthrough that starts from a fresh state directory is [mac-e2e-test.md](mac-e2e-test.md). This page is the API and the OAuth client setup. It does not repeat the chat, LAN, or admin steps.

Gmail and Drive are read-only. Calendar can list calendars and can read or change events. A calendar change is one approval for one event. Nothing is written, updated, or deleted until the owning household account approves that action. Approvals expire. There is no batch approval.

The no-NIC guest never imports this module, never sees a token, and never opens a socket to Google. The guest can ask the host to propose a calendar change. The host connector is the only component that calls Google. Fetched text is untrusted. It is not written into a guest workspace by this module. A VM task may receive it only after the owning account approves that task separately.

## API

```python
from ssios.google_connector import GoogleConnector, member_session, admin_session

connector = plane.google  # or GoogleConnector(plane, store, endpoints)
session = member_session(authenticated_account_id)
```

Build `member_session` only from the login session. Do not take an account id from an admin form and pass it to `connect`, `status`, `disconnect`, `propose_fetch`, or `take_result`. An `admin_session` is rejected by those methods.

| Call | Who | Returns |
| --- | --- | --- |
| `install_client(session, client_id, client_secret)` | member | `{"account_id", "stored": true}`. The secret is stored and not echoed. |
| `connect(session, connector, scope=None, open_browser=True)` | member | `{"authorization_url", "awaiting_consent": true}`. `connector` is `drive`, `gmail`, or `calendar`. PKCE loopback on `127.0.0.1`. No token, no verifier. Each name requests only that connector's scopes. |
| `finish_connect(session, connector)` | member | `status` after the browser returns. |
| `status(session)` | member | `service`, `connected`, `connected_at`, and `connectors`. |
| `admin_connection_status(account_id)` | admin console | the same fields. No Google call and no content. |
| `disconnect(session, connector)` | member | `status` after revoke and deletion of that connector's keychain items. |
| `read_now(session, project_id, tool, arguments, limits)` | member | the read result. Chat and `connector-fetch` use this for Gmail, Drive, and Calendar reads. It does not create an approval. |
| `propose_fetch(session, project_id, tool, arguments, limits)` | member | `{"approval": ...}` for a calendar write. Google is not called until that approval is granted. The action is `connector.write`. |
| `preview(approval, session)` | owning member | the approval card. Pass the member session. An admin session is `connector_admin_denied` and the error text has no event content. |
| existing `plane.decide(...)` | owning account | approval record only. The mail, file, or event body is not in this response. |
| `take_result(session, approval_id)` | same member | `{"text": ...}` once. Reads include the untrusted text. Writes return `created`, `updated`, or `deleted` plus the event id, not the title. Another account gets `not_found`. An admin session gets `connector_admin_denied`. |
| `revoke_and_delete(account_id)` | account deletion and admin password reset | `{"account_id", "deleted": true, "revoke_ok"}`. Revokes Gmail, Drive, and Calendar at Google, then deletes the whole `ssios.google.<account_id>` secret service. No token and no content. |

Tokens and the OAuth client secret are stored through the `keyring` package under the service `ssios.google.<account_id>`. That library talks to the macOS Keychain or the Linux Secret Service in-process. Secrets are not written to a file and are not passed on a command line. If `keyring` is missing, SSIOS raises `secure_store_unavailable` and tells you to run `pip install keyring`. If the macOS login keychain is locked, or the UI cannot show a Keychain prompt, Keychain returns `-25308` (`errSecInteractionNotAllowed`) and SSIOS raises `keychain_locked`. Unlock it in a local GUI login session with `security unlock-keychain ~/Library/Keychains/login.keychain-db`, then save the client again. `./scripts/ssios start` uses `nohup` and a background job. It does not call `setsid` and it does not install a launchd agent, so the UI stays in the login session of the shell that started it. That is not enough for a Keychain dialog: an SSH session, or a background process that cannot present UI, gets `keychain_locked`. Start from Terminal.app on the Mac. `./scripts/ssios fg` keeps the process in that terminal when a prompt has to be answered. `./scripts/ssios diagnose` writes one throwaway non-secret keychain item, reads it back, and deletes it. One Google identity is kept per SSIOS account. It is the account address from Gmail users.getProfile, Drive about.get, or, when calendar list access was granted, Calendar calendarList.list. Without that optional scope, or when calendarList.list returns 403, identity uses events.list on the primary calendar with maxResults=1. calendars.get is not used. `gmail.readonly`, `drive.readonly`, and `calendar.events` are required. `calendar.calendarlist.readonly` is optional: if Google omits it, the token is stored and the Calendar card says only the primary calendar is available. Omitting a required scope, or a PERMISSION_DENIED for insufficient scopes, still revokes the token and raises connector_scope_missing. A second consent for a different Google user is revoked and not stored. A token-endpoint `invalid_client` becomes `connector_invalid_client` and tells you to re-upload the Desktop client JSON. `invalid_grant` becomes `connector_invalid_grant` and tells you to click Connect again, because the code expired or was already used. Each of those writes one line to the UI log: account id, service, endpoint name, HTTP status, and Google's `error` field. That line does not include a code, a token, a secret, or a URL.

Gmail and Drive scopes are `https://www.googleapis.com/auth/gmail.readonly`, `https://www.googleapis.com/auth/gmail.metadata`, `https://www.googleapis.com/auth/drive.readonly`, and `https://www.googleapis.com/auth/drive.metadata.readonly`. Anything wider is rejected. `gmail.metadata` cannot request a message body. Connecting Calendar does not add these scopes, and connecting Gmail or Drive does not add Calendar scopes.

Calendar uses two scopes and does not request `https://www.googleapis.com/auth/calendar`. That full scope can also create and delete calendars and change sharing. The two narrower scopes are enough:

- `https://www.googleapis.com/auth/calendar.events` reads and writes events.
- `https://www.googleapis.com/auth/calendar.calendarlist.readonly` lists calendars the user already has. Listing does not need the read/write calendar-list scope.

If Google returns a scope that was not in that connector's request, SSIOS revokes the new refresh token and raises `oauth_scope_widened`. It does not store the token. `include_granted_scopes` is not set, so an older Gmail or Drive grant is not folded into the Calendar token.

`status` and `admin_connection_status` keep `service`, `connected`, and `connected_at` for the account. `connected` is true when any of the three connectors has a refresh token. `connectors` is a map of `drive`, `gmail`, and `calendar`, and each entry is only `connected` and `connected_at`. That is the per-service toggle. It has no titles, attendees, or tokens.

Gmail and Drive tools are `drive.search`, `drive.read_file`, `gmail.search`, and `gmail.get_message`. Calendar tools are `calendar.list_calendars`, `calendar.list_events`, `calendar.get_event`, `calendar.create_event`, `calendar.update_event`, and `calendar.delete_event`. Any other name is rejected before a request is sent. A Gmail read uses `in:inbox` and at most 50 messages, then `format=metadata` for From, Subject, and Date. A question about senders is counted from those headers. The log line for each call keeps the operator shape, the status, and the count. It does not keep the sentence, a subject, or an address. Drive root, My Drive, and 根目录 use `'root' in parents`. A folder name is resolved with `mimeType` folder and `name`. Several matches are listed and not guessed. Recent files and a file name do not need a folder id. A missing folder is `drive_intent_invalid`. If a grant limits Drive to specific folders, the error is `drive_grant_limited` and it points at Connections. Text is prefixed as untrusted external content. HTML is reduced to text. Binaries are not forwarded.

`[connectors] enabled` must list `drive`, `gmail`, and/or `calendar`. An empty list denies every fetch. Enabling Drive does not enable Calendar.

## Calendar reads and writes

`calendar.list_events` requires `time_min` and `time_max` in the arguments (or in `limits`). Both are RFC3339 timestamps with a time zone. The window must be longer than zero and at most 31 days. A missing, naive, or longer window raises `connector_window_denied` and does not create an approval. `max_items` is still capped at 20.

Each write is one event. A payload with `events` or `items` raises `connector_grant_denied`. The chat UI should call `preview(approval, session)` and show that string on the approval card.

| Tool | Arguments | What you see |
| --- | --- | --- |
| `calendar.list_calendars` | none | A calendar list. No approval. |
| `calendar.list_events` | `calendar_id`, `time_min`, `time_max` | One block per query. The heading is that window, for example `Nov 11, 2026 — no events`. |
| `calendar.get_event` | `calendar_id`, `event_id` | That event. No approval. |
| `calendar.create_event` | `calendar_id`, `title`, `start`, `end`, `time_zone`, optional `description`, `location`, `attendees`, `event_id`, `send_updates` | Calendar, title, start, end, time zone, location, attendees, and whether attendees are emailed. |
| `calendar.update_event` | `calendar_id`, `event_id`, `before`, `after`, optional `send_updates` | Before and after for those same fields. |
| `calendar.delete_event` | `calendar_id`, `event_id`, `title`, `start`, `end`, `time_zone`, optional location and attendees | Starts with `DESTRUCTIVE:`. |

`before` and `after` use `title`, `description`, `location`, `start`, `end`, `time_zone`, and `attendees`. Start and end are RFC3339 with a zone. `send_updates` is `none` (the default), `all`, or `externalOnly`. The default tells Google `sendUpdates=none`, so attendees are not emailed. The card says `Notifications: none. Attendees are not emailed.` A value of `all` or `externalOnly` is sent only when that exact value is in the approved payload, and the card says attendees will be emailed.

Create stores a client event id on the approval. If the caller omits `event_id`, SSIOS generates one before the payload hash. Google receives that id. A second approval with the same id, or a retry after Google already created it, does not create another event. Approving the same approval twice raises `approval_replay_denied`. An expired approval raises `approval_expired` and does not write. A denied approval does not write.

Update reads the event only after approval. If it does not match `before`, SSIOS raises `connector_conflict` and does not send the update. Delete is not undone by SSIOS.

A read is written to the security audit as the service name and a count. It has no approval id and no event text. A write still stores the operation (`create`, `update`, or `delete`), the account, the approval id, the tool, a SHA-256 of the calendar id, the event id, and the counts. Neither line stores titles, descriptions, locations, attendees, or tokens.

## Chat UI wiring

Build `member_session` from the logged-in member. Show one toggle per entry in `status(session)["connectors"]`. Connect and disconnect with `"calendar"`. A read calls `read_now` and is shown in the chat as its own block, with that query's time range as the heading. A write calls `propose_fetch`, shows `preview(approval, session)`, and calls `take_result` only after that same member approves. Do not call Google from the browser or from the guest. Do not auto-approve a second event because the first one was approved. An admin page should call only `admin_connection_status`.

## Mac setup

Do this on the Mac that runs SSIOS. The browser consent has to be completed by you. Nothing in this repository can approve Google access for you.

1. Enable these APIs on the Cloud project. A disabled API comes back from Google as HTTP 403 `PERMISSION_DENIED`, and SSIOS reports `connector_api_disabled`.

   - [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com)
   - [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)
   - [Google Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)

2. Open Google Auth Platform, then Data Access, and add these scopes exactly. Do not add `https://www.googleapis.com/auth/calendar`. SSIOS rejects that full scope.

   Required:

   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/drive.readonly`
   - `https://www.googleapis.com/auth/calendar.events`

   Optional. Add it when you want the account to list calendars other than the primary one. If it is not on Data Access, Google will not offer it, and SSIOS still connects Calendar for the primary calendar:

   - `https://www.googleapis.com/auth/calendar.calendarlist.readonly`

3. Leave the project in Testing. Add only the Google account you will use as a test user. Create an OAuth client of type Desktop app. Do not publish the app to skip verification.
4. `./scripts/ssios setup` installs the keyring package into `.venv`. On the Connections page, signed in as that account, save the client id and secret or upload the Desktop `client_secret_*.json` file. The same `install_client` path stores them in the Keychain. The page shows only the last characters of the client id. The secret is not written into the repo, the state directory, the config, or the audit log, and it is not passed to a shell command. The command-line prompt does the same thing. If this shell is SSH, run `security unlock-keychain ~/Library/Keychains/login.keychain-db` before `./scripts/ssios start`, or the Keychain returns `-25308`.

```bash
./scripts/ssios oauth-install-client --account YOUR_ACCOUNT_ID
```

The wrapper adds the state directory and `$SSIOS_STATE/ollama.toml`. The prompt reads the secret without echoing it. `YOUR_ACCOUNT_ID` is the id on the Admin row, `acct-` plus 16 hex digits. Use that account's own id.

5. In the shared config, under `[connectors]`, set `enabled = ["drive", "gmail", "calendar"]` for the services this Mac may use. A name left out stays denied for every account, and the Connections page shows that toggle as disabled. Each signed-in account then turns its own services on from the Connections page. That choice is stored under the account's state, not in the shared TOML. A service works only when both switches are on. Restart SSIOS if you change the shared config while it is running.
6. On the Connections page, choose Connect for a service that is turned on, or start consent from the command line. A browser opens. Sign in and approve the requested scope yourself:

```bash
./scripts/ssios oauth-connect --account YOUR_ACCOUNT_ID --connector drive
```

Repeat with `--connector gmail` for mail and `--connector calendar` for Calendar. Each consent asks only for that connector. The Google user must be the same one already linked to the SSIOS account. A different Google user is refused. Calendar's consent URL requests the events scope and the read-only calendar list scope, not the full calendar scope.

From another computer on the LAN, Connect opens Google in that browser. Google then redirects to `http://127.0.0.1:<port>/oauth/callback` on the Mac. That address does not load in the other browser. Copy the full redirect address from the address bar and paste it into Finish consent on the Connections page. The state check stays on the Mac. Enter submits the paste.

7. Check the connection. The output is only the service name, whether it is connected, and when:

```bash
./scripts/ssios oauth-status --account YOUR_ACCOUNT_ID
```

8. Read something. This example lists files in one folder. Replace the folder id with one from your own Drive. A read does not ask for approval. The text is marked untrusted.

```bash
./scripts/ssios connector-fetch \
  --account YOUR_ACCOUNT_ID \
  --project local-summary \
  --tool drive.search \
  --arguments '{"q":"trashed = false"}' \
  --limits '{"folder_ids":["YOUR_FOLDER_ID"],"max_items":5,"max_bytes":4096,"max_calls":20}'
```

A calendar list uses the same command with `--tool calendar.list_events`. Replace the window with a range of at most 31 days. The command calls Google and prints the events. It does not wait for `yes`. A create, update, or delete uses the same command, prints the approval card, and waits. Type `yes` to let the host call Google. The card is the change you are approving. `send_updates` defaults to `none`.

```bash
./scripts/ssios connector-fetch \
  --account YOUR_ACCOUNT_ID \
  --project local-summary \
  --tool calendar.list_events \
  --arguments '{"calendar_id":"YOUR_CALENDAR_ID","time_min":"2026-11-01T00:00:00Z","time_max":"2026-11-20T00:00:00Z"}' \
  --limits '{"max_items":5,"max_bytes":4096,"max_calls":20}'
```

9. Disconnect one service, or wipe the account's Google secrets after a password reset or account deletion:

```bash
./scripts/ssios oauth-disconnect --account YOUR_ACCOUNT_ID --connector drive
./scripts/ssios oauth-revoke-account --account YOUR_ACCOUNT_ID
```

`oauth-revoke-account` is what account deletion and an admin password reset must call. It revokes the Gmail, Drive, and Calendar refresh tokens at Google and deletes the keychain items, including the Calendar ones. It does not print them. An admin still cannot read the member's mail, files, or calendar. The member has to consent again.

The chat UI should show the approval card from `preview(approval, session)` and call `take_result` only with the member session that owns the approval. Fetched text may be sent to local Ollama. It must not be placed in a microVM unless a separate approval puts it in that account's workspace. A calendar write is not a guest tool.

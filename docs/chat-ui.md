# Chat, household accounts, and the home network

This page is the run guide for the local chat UI. It was not executed on the Apple M4 as part of the automated tests. Those tests use a fake Ollama client and a fake guest. A real microVM boot is still the `macos-vz` smoke on the Mac, and this page does not claim that smoke was run again.

## What the chat does

Sign in with a household account. Each account has its own conversation files, MemFS repository, tasks, approvals, workspace, and Gmail/Drive connection. The files live under the state directory you pass to `--state`. They are not part of the git repository.

The router prints its choice and the reason on the turn:

- A plain question goes to local Ollama. No guest is started.
- A plain question, in Chinese or English, goes to local Ollama. A request to run code, edit or create files, or touch a workspace starts the no-NIC Codex guest named by `[runtime] backend` and `agent`. On a Mac that pair is `macos-vz` and `codex`. On Linux it is `cloud-hypervisor` and `codex`. `dev-guard` and `summary` are refused before any boot line. If the guest cannot start, the assistant message shows the English reason, an error code, and a hint, and that message stays in the account's chat history. Codex is not run on the host. `ssios vm-diagnose` prints a redacted summary of the backend, vfkit, kernel, initramfs, Codex binary, Ollama, and the last guest error. It does not print tokens.
- If the router is not sure the message is only a question, it uses the guest.
- Gmail and Drive stay read-only. Calendar can list calendars and events, and can create, update, or delete one event. None of those calls happen until you approve the card. A calendar write is performed by the host connector. A calendar request stays on that connector path unless the same message also asks for a workspace change.
- Force direct and Force virtual machine override the router. Force direct still does not run tools on the host. A cloud read still waits for approval.

Nothing from a guest is applied until the owning account approves the card. An admin cannot approve another account's card, and cannot read that account's chat, memory, or mail.

## Start on the Mac

Ollama stays on `127.0.0.1:11434`. `./scripts/ssios` uses `$HOME/Library/Application Support/ssios` and `$SSIOS_STATE/ollama.toml`. It uses the repo `.venv` when that directory exists. It does not read or write `.ssios`, `.ssios-demo`, or `.ssios-memfs`.

```bash
./scripts/ssios setup
```

`setup` creates `.venv` if needed, installs `keyring`, and copies `config/ssios.example.toml` only when the config is missing or empty. Edit the copy: set `provider` to `ollama` and `ollama_model` to a local name. There is no cloud fallback. On a 24GB M4, `qwen2.5:7b` is the model to use for calendar sentences in Chinese and English. `qwen2.5-coder:7b` remains a good local model for other chat. Set `[ui] time_zone` to `America/Los_Angeles` on Wenfeng's Mac. An empty value uses the computer's local zone. Do not put an OAuth client secret in the file.

```bash
./scripts/ssios user add --admin --username owner
```

Type the password twice. It is hashed with scrypt and is not printed.

```bash
./scripts/ssios start
./scripts/ssios status
./scripts/ssios logs
./scripts/ssios restart
./scripts/ssios stop
```

`start` runs the UI in the background and prints the URL from `listen.url`. `restart` stops that process and starts it again. `stop` signals only the process recorded in `$SSIOS_STATE/run/ui.pid` after checking that it is this UI. `logs` follows `$SSIOS_STATE/run/ui.log`. Open the printed URL and sign in. Chat is at `/ui/chat`. `./scripts/ssios fg` runs the same server in this terminal.

- Direct answer: send `What is a microVM?` and leave the route on Auto. The badge says `direct`. The conversation stays in the sidebar for that account. Reopening the page, or restarting the server, reloads the messages, the route, and any approval card with its current state. Another account cannot see it.
- Guest task: send `edit readme.md`. The turn shows booting, running, and the isolation checks, then an approval card. Approving records the result. It does not run the change on the host outside the account workspace.

`ssios up` still serves the same UI on loopback. `ssios ui` is the same server with the `--lan` switch below.

## Home network (opt-in)

The default bind is `127.0.0.1`. To serve other computers on the home LAN:

```bash
./scripts/ssios restart --lan
```

The same choice can be saved as `[ui] bind = "0.0.0.0"` in the config copy. Any other bind address is rejected.

When LAN mode is on, the server speaks HTTPS. The first start writes a self-signed certificate under `<state>/ui/tls/` (mode `0600`) and prints the SHA-256 fingerprint. Compare that fingerprint with the browser warning before you continue. To use mkcert instead, put `cert.pem` and `key.pem` in that directory before starting. They are used as-is and are not overwritten.

Startup prints `https://<hostname>.local:<port>/ui/login` and the same path on each private IP it can see. Opening the site root in a browser goes to that sign-in page. Only loopback, RFC1918, IPv4 link-local, IPv6 unique-local, and IPv6 link-local clients are accepted. Other source addresses are rejected. The Host and Origin must match this server, and browser origins are not given CORS headers.

macOS may show a firewall prompt the first time the process listens on all interfaces. Allow incoming connections if a browser on another computer on the home network should connect. If you deny the prompt, a browser on the Mac itself can still use the loopback URL.

Every page, including the chat event stream, requires a signed-in account. Login attempts are rate-limited. The session cookie is HttpOnly and SameSite=Strict, and it is marked Secure while HTTPS is on. State-changing requests, including approvals and Gmail/Drive connect or disconnect, require the CSRF token from the signed-in page.

## Accounts and the admin console

```bash
./scripts/ssios user add --username ada
```

That host command creates an active member. A person can also request an account at `/ui/signup`, or open a one-time invite created in the admin console. Those accounts stay pending until an admin approves them. Sign-up is rate-limited. A request cannot choose the admin role.

The admin console is `/ui/admin`. An admin can approve or reject a request, enable or disable an account, assign admin or member, reset a password, or delete an account. Delete asks you to type `delete <username>`. It does not delete silently.

Password reset revokes that account's sessions and its Google connections. The member must choose a new password and connect Gmail or Drive again. Resetting a password and signing in as that member does not reveal old mail, because the tokens are revoked first.

Delete revokes the Google tokens, deletes the keychain items for `ssios.google.<account id>`, then moves that account's local directory to `<state>/accounts/archive/<account id>-<time>/` (mode `0700`). The archive is not served by the UI. The original single-user memory tree, which belongs to the first admin, is not moved.

An admin sees whether Gmail or Drive is connected, and the connected-at time. The console does not show tokens, message bodies, Drive file contents, conversations, or memory. There is no impersonation.

`/ui/audit` is the signed-in account's own activity. `/ui/admin/audit` is the full log, with filters and an export. `ssios audit-verify` checks the hash chain. The log records logins, sign-ups, account decisions, connection and fetch metadata (service, item ids, counts), guest start and finish, and routing decisions. It does not record message text, memory text, mail, file contents, tokens, or passwords.

## Calendar in ordinary language

Say what you want in Chinese or English. The local Ollama model only fills a small JSON intent. It does not choose the calendar date. The program resolves "this week", "tomorrow afternoon", "下周三", and "周五" from today's date and the account time zone, then the approval card shows the absolute date, time, and zone.

Examples:

- `这周我有什么安排`
- `明天下午有空吗`
- `下周三下午三点和张三开会一小时`
- `把周五的牙医改到下午四点`
- `取消明天的站会`
- `What's on my calendar next week?`

Rules shown on the card:

- A week is Monday 00:00 through the next Monday 00:00.
- Next week, and 下周三, means that day in the following week.
- A bare 周五 or Friday means the next one, and today counts when today is that weekday. If the clock time has already passed, the card still uses today and says so.
- A read with no range is today through seven days. No window can be longer than 31 days.
- A name such as 张三 is kept in the title and description. The program does not invent an email address. Attendees are not emailed unless you explicitly ask to notify them.
- Changing or cancelling an event first asks you to approve a bounded read. If more than one event matches, reply with a number. A second card is the only thing that can change or delete the event.

The older field form still works: `list calendar events calendar:primary from:2026-11-10T00:00:00Z to:2026-11-17T00:00:00Z` and `create calendar event title:SSIOS-test start:2026-11-12T15:00:00Z end:2026-11-12T15:30:00Z zone:UTC calendar:primary send:none`. If Ollama is down, or its JSON is still invalid after one retry, chat shows that in English and suggests this form. It does not read or change the calendar.

## Gmail and Drive

The chat UI calls the connector in `src/ssios/google_connector.py`. The API, scopes, and keyring setup are in [google-connector.md](google-connector.md). A single Mac walkthrough that uses a fresh state directory is in [mac-e2e-test.md](mac-e2e-test.md).

The signed-in account, whether member or admin, can connect, disconnect, and propose a read or a calendar change for that same account. An admin still cannot open or approve another account's cards, and cannot read that account's tokens or mail. The Connections page shows Gmail, Drive, and Calendar separately from `status(session)["connectors"]`. Connecting Calendar does not mark Gmail or Drive connected. The admin console uses `admin_connection_status` for other accounts and sees only connected and connected_at for each connector. Password reset and account deletion call `revoke_and_delete`, which includes Calendar. Chat uses `propose_fetch`. After the owning account approves, `take_result` shows the read text or `created` / `updated` / `deleted` plus the event id. A calendar update card shows the before and after text. A delete card is marked destructive. Attendees are not emailed unless `send_updates` is `all` or `externalOnly`.

Connect from another computer shows the consent URL. Open it in a browser on the Mac. Google redirects to `127.0.0.1` on that Mac. The callback is not bound to the LAN address. The OAuth client is installed with `./scripts/ssios oauth-install-client --account kent`. The account value is the id shown on the Admin row. The command reads the secret from a hidden prompt. It is stored by the `keyring` package, not on a command line and not in a file. The wrapper does not print that secret.

## Tests

```bash
make test
```

The chat tests use an in-memory secret store and a fake guest. They do not call Google and they do not boot a virtual machine.
